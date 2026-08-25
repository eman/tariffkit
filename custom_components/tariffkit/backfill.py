"""Price metered history into Home Assistant's long-term statistics.

The running-total entities compute forward from the moment meters are
configured. Everything before that is unreachable inside Home Assistant, even
when the recorder holds every reading needed to price it -- which is the common
case for anyone who was on the tariff before they found the setting.

This writes that history as **external statistics** under a ``tariffkit:``
namespace rather than into the entities' own series, which is how
``homeassistant.components.opower`` publishes utility history and is the right
shape for three reasons. There is no seam to reconcile with what the live path
is writing; a rerun replaces a period rather than appending to it, so the whole
window can be recomputed whenever the account history changes underneath it; and
the recorder's own hourly compilation of the entities is left entirely alone.

Granularity is one row per day, and deliberately so. A bill is a daily and
cyclical artefact: the Base Services Charge is per day, the energy surcharge is
floored per day, and the baseline allowance is granted per cycle and consumed in
day order. Only the energy charges and export credits are additive per hour, so
an hourly series would have to invent an attribution for everything else. A day
is the finest slice this can state exactly, and stating it exactly is the point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from tariffkit.account import AccountProfile
from tariffkit.billing import Bill, BillingPeriod, IntervalReading
from tariffkit.timeutil import PACIFIC

from .energy import price, resolve_cycle, statement_periods

_LOGGER = logging.getLogger(__name__)

DOMAIN_SOURCE = "tariffkit"
MONEY_UNIT = "USD"


@dataclass(frozen=True, slots=True)
class Series:
    """One statistic this publishes, and how to read it off a day's bill."""

    slug: str
    name: str
    unit: str
    unit_class: str | None

    def statistic_id(self, profile_name: str) -> str:
        return f"{DOMAIN_SOURCE}:{profile_name}_{self.slug}"

    def metadata(self, profile_name: str) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_mean=False,
            has_sum=True,
            name=f"TariffKit {profile_name} {self.name}",
            source=DOMAIN_SOURCE,
            statistic_id=self.statistic_id(profile_name),
            unit_of_measurement=self.unit,
            unit_class=self.unit_class,
        )


SERIES: tuple[Series, ...] = (
    Series("grid_import", "grid import", UnitOfEnergy.KILO_WATT_HOUR, "energy"),
    Series("grid_export", "grid export", UnitOfEnergy.KILO_WATT_HOUR, "energy"),
    Series("energy_cost", "energy cost", MONEY_UNIT, None),
    Series("export_credit", "export credit", MONEY_UNIT, None),
    Series("net_cost", "net cost", MONEY_UNIT, None),
)


@dataclass(slots=True)
class DayFigures:
    """One day's exact contribution to its cycle."""

    day: date
    grid_import: float = 0.0
    grid_export: float = 0.0
    energy_cost: float = 0.0
    export_credit: float = 0.0
    net_cost: float = 0.0

    def value(self, slug: str) -> float:
        return float(getattr(self, slug))


@dataclass(slots=True)
class BackfillResult:
    """What a run wrote, and what it could not."""

    days: list[DayFigures] = field(default_factory=list)
    #: Cycles the account history could not price, with the reason.
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self, profile_name: str) -> dict[str, object]:
        return {
            "days": len(self.days),
            "first_day": self.days[0].day.isoformat() if self.days else None,
            "last_day": self.days[-1].day.isoformat() if self.days else None,
            "grid_import_kwh": round(sum(d.grid_import for d in self.days), 3),
            "grid_export_kwh": round(sum(d.grid_export for d in self.days), 3),
            "energy_cost": round(sum(d.energy_cost for d in self.days), 2),
            "export_credit": round(sum(d.export_credit for d in self.days), 2),
            "net_cost": round(sum(d.net_cost for d in self.days), 2),
            "statistic_ids": [s.statistic_id(profile_name) for s in SERIES],
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
        }


def cycles_between(
    profile: AccountProfile, opens: date, closes: date, start_day: int
) -> list[BillingPeriod]:
    """The billing cycles covering ``opens``..``closes``, oldest first.

    Statement evidence fixes the boundaries wherever the profile has any, so a
    backfilled cycle lines up with a real bill rather than with a guessed day of
    the month. Where it has none the configured meter-read day carries it, which
    is the same fallback the running totals use.
    """
    periods = statement_periods(profile)
    found: list[BillingPeriod] = []
    cursor = closes
    while cursor >= opens:
        cycle = resolve_cycle(cursor, start_day, periods)
        first = max(cycle.start, opens)
        found.append(BillingPeriod(first, cursor))
        if cycle.start <= opens:
            break
        cursor = cycle.start - timedelta(days=1)
    return list(reversed(found))


def _day_of(reading: IntervalReading) -> date:
    return reading.start.astimezone(PACIFIC).date()


def price_cycle(
    profile: AccountProfile,
    readings: list[IntervalReading],
    cycle: BillingPeriod,
) -> tuple[list[DayFigures], str, tuple[str, ...]]:
    """Each day's exact share of one cycle.

    Walks the cycle day by day, differencing consecutive cycle-to-date bills.
    A day's figures are therefore its *marginal* contribution, which is the only
    decomposition that both sums to the cycle and never exceeds it: the baseline
    allowance is cycle-cumulative, so pricing a day in isolation would grant it
    one day's worth however much the cycle had banked.
    """
    within = [r for r in readings if cycle.contains(r.start)]
    days: list[DayFigures] = []
    previous: Bill | None = None
    warnings: tuple[str, ...] = ()
    day = cycle.start
    while day <= cycle.end:
        period = BillingPeriod(cycle.start, day)
        so_far = [r for r in within if _day_of(r) <= day]
        running, reason = price(profile, so_far, period)
        if running is None:
            return [], reason, ()
        warnings = running.warnings
        metered = [r for r in within if _day_of(r) == day]
        days.append(
            DayFigures(
                day=day,
                grid_import=sum(r.imported for r in metered),
                grid_export=sum(r.exported for r in metered),
                energy_cost=_delta(running, previous, "charges"),
                export_credit=-_delta(running, previous, "credits"),
                net_cost=_delta(running, previous, "total"),
            )
        )
        previous = running
        day += timedelta(days=1)
    return days, "", warnings


def _delta(running: Bill, previous: Bill | None, part: str) -> float:
    def read(bill: Bill | None) -> float:
        if bill is None:
            return 0.0
        if part == "charges":
            return bill.energy_charges + bill.taxes
        if part == "credits":
            return bill.export_credits
        return bill.total

    return read(running) - read(previous)


def build(
    profile: AccountProfile,
    readings: list[IntervalReading],
    opens: date,
    closes: date,
    start_day: int,
) -> BackfillResult:
    """Price every day between ``opens`` and ``closes``, cycle by cycle."""
    result = BackfillResult()
    for cycle in cycles_between(profile, opens, closes, start_day):
        days, reason, warnings = price_cycle(profile, readings, cycle)
        if reason:
            # One unpriceable cycle does not sink the rest: a profile whose
            # history begins mid-window should still get everything after it.
            result.skipped.append(f"{cycle.start}..{cycle.end}: {reason}")
            continue
        result.days.extend(days)
        result.warnings.extend(w for w in warnings if w not in result.warnings)
    return result


def statistics_for(result: BackfillResult, series: Series) -> list[StatisticData]:
    """One cumulative row per day, as external statistics want it.

    ``sum`` runs from the start of the window rather than from any earlier
    series, because a rerun rewrites the window whole. ``state`` carries the
    day's own figure, which is what a statistics card labels each bar with.
    """
    running = 0.0
    rows: list[StatisticData] = []
    for figures in result.days:
        value = figures.value(series.slug)
        running += value
        rows.append(
            StatisticData(
                start=datetime(
                    figures.day.year, figures.day.month, figures.day.day, tzinfo=PACIFIC
                ),
                state=round(value, 6),
                sum=round(running, 6),
            )
        )
    return rows


async def async_publish(hass: HomeAssistant, profile_name: str, result: BackfillResult) -> None:
    """Write every series, replacing whatever occupied those days before."""
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    for series in SERIES:
        rows = statistics_for(result, series)
        if not rows:
            continue
        async_add_external_statistics(hass, series.metadata(profile_name), rows)
        _LOGGER.debug("wrote %d rows to %s", len(rows), series.statistic_id(profile_name))
