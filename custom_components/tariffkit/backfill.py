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

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from tariffkit.account import AccountProfile
from tariffkit.billing import Bill, BillingPeriod, IntervalReading
from tariffkit.timeutil import PACIFIC

from .energy import coverage_warnings, price, resolve_cycle, statement_periods

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

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
        return f"{DOMAIN_SOURCE}:{statistic_slug(profile_name)}_{self.slug}"

    def metadata(self, profile_name: str) -> StatisticMetaData:
        from homeassistant.components.recorder.models import (
            StatisticMeanType,
            StatisticMetaData,
        )

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


def statistic_slug(profile_name: str) -> str:
    """A profile name as a statistic id can actually carry.

    Home Assistant's ``VALID_STATISTIC_ID`` allows only lowercase letters,
    digits and single underscores. Profile names allow hyphens, and the config
    flow *creates* them -- it slugifies "My Home" to ``my-home`` -- so publishing
    the raw name fails at the last step of an otherwise complete run with a bare
    "Invalid statistic_id". ``opower`` folds hyphens the same way.
    """
    name = profile_name.strip().lower()
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name)).strip("_")
    if slug == name:
        return slug
    # Folding is lossy: `my-home`, `my_home` and `my__home` are three profiles
    # Home Assistant keeps apart, and all three fold to `my_home`. Backfilling
    # one would then overwrite another's series. A short digest of the original
    # name keeps them distinct, and only names that actually needed folding pay
    # for it -- an already-valid name keeps its own spelling.
    return f"{slug}_{hashlib.sha256(name.encode()).hexdigest()[:6]}"


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
    #: Cycles that could not be priced, with the reason.
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
            "complete": not self.skipped and not self.warnings,
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
        # The cycle's *true* start, never clipped to the window. Truncating a
        # cycle's tail is harmless -- `bill(start..D)` does not depend on days
        # after D -- but clipping its head silently discards whatever baseline
        # allowance the real cycle had banked, and every day of it then prices
        # too high. A leading cycle the window does not fully cover is refused
        # by `build` rather than quietly mispriced.
        found.append(BillingPeriod(cycle.start, cursor))
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

    Walks the cycle day by day, differencing consecutive cycle-to-date bills, so
    a day's figures are its *marginal* contribution. That is the decomposition
    that sums to the cycle: the baseline allowance is cycle-cumulative, so
    pricing a day in isolation would grant it one day's worth however much the
    cycle had banked. It does not bound a day by the cycle -- a heavy import day
    inside a month that exports for the rest costs more on its own than the
    whole cycle does.

    Days the readings say nothing about are not emitted. A day the recorder
    holds no reading for is not a day of zero usage, and pricing it would put a
    daily fixed charge on a day this has no evidence about. Skipping it does not
    disturb the others: each remaining day is still its own marginal
    contribution, since the skipped day's charges cancel between the two
    cycle-to-date bills that bracket it.
    """
    within = _within(readings, cycle)
    seen = {_day_of(reading) for reading in within}
    days: list[DayFigures] = []
    previous: Bill | None = None
    warnings: tuple[str, ...] = ()
    unmetered: list[date] = []
    day = cycle.start
    while day <= cycle.end:
        period = BillingPeriod(cycle.start, day)
        so_far = [r for r in within if _day_of(r) <= day]
        running, reason = price(profile, so_far, period)
        if running is None:
            return [], reason, ()
        warnings = running.warnings
        metered = [r for r in within if _day_of(r) == day]
        if day in seen:
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
        else:
            unmetered.append(day)
        previous = running
        day += timedelta(days=1)
    if unmetered:
        warnings = (
            *warnings,
            f"{len(unmetered)} day(s) inside {cycle.start}..{cycle.end} have no metered "
            f"readings ({unmetered[0]}..{unmetered[-1]}) and were left unpriced rather "
            f"than charged a daily charge there is no evidence for",
        )
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


def evidence_span(readings: list[IntervalReading]) -> tuple[date, date] | None:
    """The first and last day any reading covers, or None for no readings.

    A day the recorder holds nothing for is not a zero day. Pricing one anyway
    charges a Base Services Charge for a day this has no evidence about, and the
    written row is then indistinguishable from a real day of no usage -- which
    matters because the default window starts at the account's first epoch,
    routinely years before the meter sensor existed.
    """
    if not readings:
        return None
    days = [_day_of(reading) for reading in readings]
    return min(days), max(days)


def build(
    profile: AccountProfile,
    readings: list[IntervalReading],
    opens: date,
    closes: date,
    start_day: int,
) -> BackfillResult:
    """Price every day the evidence covers, cycle by cycle."""
    result = BackfillResult()
    span = evidence_span(readings)
    if span is None:
        result.skipped.append(
            f"{opens}..{closes}: the recorder holds no readings for these meters "
            f"over this window, so there is nothing to price"
        )
        return result
    first_seen, last_seen = span
    if first_seen > opens:
        result.warnings.append(
            f"no metered readings before {first_seen}, so {opens}..{first_seen} was "
            f"not priced rather than being charged a daily charge it has no evidence for"
        )
    if last_seen < closes:
        result.warnings.append(f"no metered readings after {last_seen}")
    opens, closes = max(opens, first_seen), min(closes, last_seen)
    if opens > closes:
        return result

    for cycle in cycles_between(profile, opens, closes, start_day):
        if cycle.start < opens:
            # A cycle joined partway through cannot be decomposed: its earlier
            # days are missing, so the later ones have nothing to be marginal to,
            # and pricing them as though the cycle began here would discard an
            # allowance the real cycle had been banking since its true start.
            result.skipped.append(
                f"{cycle.start}..{cycle.end}: the window starts inside this cycle "
                f"({opens}), so its days cannot be priced against a full cycle. "
                f"Backfill from {cycle.start} or earlier to include it"
            )
            continue
        days, reason, warnings = price_cycle(profile, readings, cycle)
        if reason:
            # `reason` already names a period, so quote only its explanation.
            detail = reason.split(": ", 1)[-1]
            result.skipped.append(f"{cycle.start}..{cycle.end}: {detail}")
            continue
        result.days.extend(days)
        for warning in (*warnings, *coverage_warnings(_within(readings, cycle), cycle)):
            if warning not in result.warnings:
                result.warnings.append(warning)
    return result


def _within(readings: list[IntervalReading], cycle: BillingPeriod) -> list[IntervalReading]:
    return [r for r in readings if cycle.contains(r.start)]


def statistics_for(
    result: BackfillResult, series: Series, base: float = 0.0
) -> list[StatisticData]:
    """One cumulative row per day, as external statistics want it.

    ``sum`` continues from ``base`` -- whatever the series already held
    immediately before this window -- because writing external statistics
    replaces only the rows it names and leaves everything earlier in place. A
    ``sum`` restarted at zero partway through a live series does not merely lose
    the earlier days: the recorder derives each period's value by differencing
    consecutive sums, so the first rewritten day reports a large negative figure
    and every aggregate over the series is wrong from there on.

    ``state`` carries the day's own figure, which is what a statistics card
    labels each bar with.
    """
    from homeassistant.components.recorder.models import StatisticData

    running = base
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


#: How far back to look for the sum a rewritten window must continue from.
#: Bounded so the lookup cannot walk an unbounded series; ten years is longer
#: than any account history this prices.
BASE_LOOKBACK = timedelta(days=3650)


async def async_base_sums(hass: HomeAssistant, profile_name: str, opens: date) -> dict[str, float]:
    """What each series already totalled immediately before ``opens``.

    Zero where a series holds nothing earlier, which is the first-run case.
    """
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.helpers.recorder import get_instance

    opens_at = datetime(opens.year, opens.month, opens.day, tzinfo=PACIFIC)
    ids = {series.statistic_id(profile_name) for series in SERIES}
    # Hourly, not daily. `statistics_during_period` aligns a day-period
    # `end_time` forward to the next local midnight, so asking for "before this
    # day" with period="day" returns that day too -- and the base would then
    # include the very row about to be rewritten, shifting the whole window by
    # one day's worth. Hourly periods are not realigned.
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        opens_at - BASE_LOOKBACK,
        opens_at,
        ids,
        "hour",
        None,
        {"sum"},
    )
    bases: dict[str, float] = {}
    for statistic_id in ids:
        sums = [row["sum"] for row in rows.get(statistic_id, []) if row.get("sum") is not None]
        bases[statistic_id] = float(sums[-1] or 0.0) if sums else 0.0
    return bases


async def async_publish(hass: HomeAssistant, profile_name: str, result: BackfillResult) -> None:
    """Write every series, continuing the sums the earlier days established.

    A window may open after rows this already wrote -- backfilling a year, then
    later only the last cycle. Writing external statistics replaces only the
    rows it names, so restarting ``sum`` at zero would splice a reset into a
    live series and make the recorder derive a large negative value for the
    first rewritten day.
    """
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    if not result.days:
        return
    bases = await async_base_sums(hass, profile_name, result.days[0].day)
    for series in SERIES:
        statistic_id = series.statistic_id(profile_name)
        rows = statistics_for(result, series, bases.get(statistic_id, 0.0))
        if not rows:
            continue
        async_add_external_statistics(hass, series.metadata(profile_name), rows)
        _LOGGER.debug("wrote %d rows to %s", len(rows), statistic_id)
