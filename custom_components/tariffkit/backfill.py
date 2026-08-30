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
from tariffkit.billing import (
    Bill,
    BillingPeriod,
    CreditBalances,
    IntervalReading,
    LedgerEntry,
    LifetimeLedger,
    apply_credits,
    run_lifetime,
)
from tariffkit.errors import TariffKitError
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
    Series("amount_due", "amount due", MONEY_UNIT, None),
)


@dataclass(slots=True)
class DayFigures:
    """One day's exact contribution to its cycle."""

    day: date
    grid_import: float = 0.0
    grid_export: float = 0.0
    energy_cost: float = 0.0
    export_credit: float = 0.0
    #: What a statement would charge for this day: its marginal share of the
    #: cycle's cash due, the bank the cycle opened with already applied.
    amount_due: float = 0.0

    def value(self, slug: str) -> float:
        return float(getattr(self, slug))


@dataclass(slots=True)
class BackfillResult:
    """What a run wrote, and what it could not."""

    days: list[DayFigures] = field(default_factory=list)
    #: One bill per priced cycle, oldest first. What a cycle earned and owed
    #: before any bank is applied; :attr:`lifetime` is what a statement states.
    bills: list[Bill] = field(default_factory=list)
    #: Those bills folded end to end, every annual settlement applied. The
    #: authority on what each cycle opened with and what it actually charged.
    lifetime: LifetimeLedger = field(default_factory=LifetimeLedger)
    #: Days inside a priced cycle that could not be published. Their energy is
    #: still in that cycle's bill -- a cumulative counter's endpoints survive an
    #: outage -- so the daily rows and the cycle disagree by exactly their share,
    #: and pretending otherwise would let two figures in one answer contradict
    #: each other silently.
    unpriced: list[date] = field(default_factory=list)
    #: Cycles that could not be priced, with the reason.
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def residual(self) -> float:
        """What the cycles hold that the daily rows do not publish.

        Non-zero whenever a day inside a priced cycle could not be published:
        its energy is still in the cycle's bill, and so is its Base Services
        Charge, which is owed for the day of service whether or not the meter
        reported anything that day.
        """
        return self.lifetime.cash_due - sum(day.amount_due for day in self.days)

    def entries(self) -> dict[BillingPeriod, LedgerEntry]:
        """Each cycle after credits are applied, keyed by the period it covers.

        This is where a cycle stops being a bill and becomes a statement: the
        ledger decides how much of the credit earned may be spent now, how much
        banks, and -- through the annual settlements :attr:`lifetime` applies --
        how much is clawed back for having already been paid out in cash.
        """
        return {entry.period: entry for entry in self.lifetime.entries}

    def summary(self, profile_name: str) -> dict[str, object]:
        entries = self.entries()
        return {
            "days": len(self.days),
            "days_unpriced": len(self.unpriced),
            # Non-zero whenever a day inside a priced cycle could not be
            # published. The `cycles` list is the authority on what a period
            # cost; the daily rows are that period minus these days.
            "residual": round(self.residual, 2),
            "first_day": self.days[0].day.isoformat() if self.days else None,
            "last_day": self.days[-1].day.isoformat() if self.days else None,
            "grid_import_kwh": round(sum(d.grid_import for d in self.days), 3),
            "grid_export_kwh": round(sum(d.grid_export for d in self.days), 3),
            "energy_cost": round(sum(d.energy_cost for d in self.days), 2),
            "export_credit": round(sum(d.export_credit for d in self.days), 2),
            "amount_due": round(sum(d.amount_due for d in self.days), 2),
            "statistic_ids": [s.statistic_id(profile_name) for s in SERIES],
            "cycles": [
                {
                    "start": bill.period.start.isoformat(),
                    "end": bill.period.end.isoformat(),
                    "days": bill.period.days,
                    "imported_kwh": round(bill.imported_kwh, 3),
                    "exported_kwh": round(bill.exported_kwh, 3),
                    "energy_charges": round(bill.energy_charges, 2),
                    "taxes": round(bill.taxes, 2),
                    "export_credits": round(-bill.export_credits, 2),
                    "fixed_charges": round(bill.fixed_charges, 2),
                    "total": round(bill.total, 2),
                    # What a statement would print. Differs from `total`
                    # wherever the cycle earned more credit than its charges
                    # could absorb, which is the bank being fed. `bank_closing`
                    # is the balance standing after it, not this cycle's own
                    # contribution to it -- the entity attribute named
                    # `bank_change` is that, and they are different numbers.
                    "cash_due": round(entry.cash_due, 2),
                    # `cash_due` is `max(0, gross_charges - credit_applied)`,
                    # and the components above reach `gross_charges` only once
                    # `in_cycle_offsets` is taken off them: anything the
                    # statement spends in-cycle instead of banking is already
                    # out of it. The clamp is load-bearing -- `gross_charges`
                    # goes negative wherever a baseline credit outweighs the
                    # charges, while `cash_due` stays at zero, because a
                    # statement charges nothing rather than paying out.
                    # Published so the block reconciles at both ends, the same
                    # terms the entity attributes carry.
                    "gross_charges": round(entry.gross_charges, 2),
                    "in_cycle_offsets": round(entry.in_cycle_offsets.total, 2),
                    "non_offsettable": round(entry.non_offsettable, 2),
                    "credit_applied": round(entry.applied.total, 2),
                    "bank_closing": round(entry.closing.total, 2),
                    "complete": bill.complete,
                }
                # Matched by period rather than by position: `run_ledger`
                # sorts what it is given, and a summary that silently pairs one
                # cycle's charges with another's balance is worse than none.
                for bill, entry in ((b, entries[b.period]) for b in self.bills)
            ],
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


@dataclass(slots=True)
class CycleWalk:
    """One cycle priced, and every cycle-to-date bill along the way.

    The running bills are kept because a day's figures are differences between
    consecutive ones, and what is actually owed on a day cannot be worked out
    until the bank this cycle opens with is known -- which needs every other
    cycle priced first. Pricing once and decomposing twice beats pricing twice.
    """

    cycle: BillingPeriod
    bill: Bill | None = None
    reason: str = ""
    #: One per day of the cycle: the day, its cycle-to-date bill, the energy its
    #: own hours moved, and whether it may be published. Every day is here, not
    #: only the publishable ones, because a decomposition differences
    #: consecutive cycle-to-date bills and a refused day still has to advance
    #: that sequence -- dropping it hands its charges to whichever day follows,
    #: which is exactly the silent disagreement the residual exists to report.
    running: list[tuple[date, Bill, float, float, bool]] = field(default_factory=list)
    warnings: tuple[str, ...] = ()
    unmetered: list[date] = field(default_factory=list)


def walk_cycle(
    profile: AccountProfile,
    readings: list[IntervalReading],
    cycle: BillingPeriod,
) -> CycleWalk:
    """Price one cycle day by day, keeping every cycle-to-date bill.

    Days the readings cannot account for are not emitted -- one with no
    readings at all, or one holding an hour that carries another day's energy.
    An hour reconstructed across an outage cannot be separated from the hour's
    own usage, because the counter reports one number, so the day it lands on
    cannot be priced, only guessed at.

    Skipping does not disturb the days around it: each remaining day is still
    its own marginal contribution, because a skipped day's charges cancel
    between the two cycle-to-date bills that bracket it. It does change what the
    emitted days *sum to*, and by more than the skipped days' energy -- a day
    with no readings still owes its Base Services Charge, which stays in the
    cycle's own bill. :attr:`BackfillResult.residual` is that difference,
    reported rather than left for someone to discover by subtracting two figures
    that were supposed to agree.
    """
    walk = CycleWalk(cycle=cycle)
    within = _within(readings, cycle)
    seen = {_day_of(reading) for reading in within}
    day = cycle.start
    while day <= cycle.end:
        period = BillingPeriod(cycle.start, day)
        so_far = [r for r in within if _day_of(r) <= day]
        running, reason = price(profile, so_far, period)
        if running is None:
            return CycleWalk(cycle=cycle, reason=reason)
        walk.warnings = running.warnings
        metered = [r for r in within if _day_of(r) == day]
        publish = day in seen and not any(reading.estimated for reading in metered)
        walk.running.append(
            (
                day,
                running,
                sum(r.imported for r in metered),
                sum(r.exported for r in metered),
                publish,
            )
        )
        if not publish:
            walk.unmetered.append(day)
        walk.bill = running
        day += timedelta(days=1)
    if walk.unmetered:
        walk.warnings = (
            *walk.warnings,
            f"{len(walk.unmetered)} day(s) inside {cycle.start}..{cycle.end} could not be "
            f"priced ({walk.unmetered[0]}..{walk.unmetered[-1]}): either no metered readings "
            f"at all, or an hour carrying a counter's catch-up across an outage, whose "
            f"energy belongs to days the tariff would price differently",
        )
    # The last cycle-to-date bill is the cycle's own. Kept because it is what a
    # statement states, and what a credit ledger folds to carry a bank between
    # cycles -- neither of which a day decomposition can do.
    return walk


def decompose(walk: CycleWalk, opening: CreditBalances | None) -> list[DayFigures]:
    """Each day's marginal share of its cycle, given the bank it opened with.

    Differences consecutive cycle-to-date bills, so a day's figures are its
    *marginal* contribution. That is the decomposition that sums to the cycle:
    the baseline allowance is cycle-cumulative, so pricing a day in isolation
    would grant it one day's worth however much the cycle had banked. It does
    not bound a day by the cycle -- a heavy import day inside a month that
    exports for the rest costs more on its own than the whole cycle does.

    ``opening`` makes the published amount the figure a statement prints rather
    than ``Bill.total``, which subtracts every credit the cycle earned. The same
    balance is used for every day, which is what cancels it out of the
    differences instead of leaving a month's bank inside one day.
    """
    days: list[DayFigures] = []
    previous: Bill | None = None
    for day, running, imported, exported, publish in walk.running:
        if publish:
            days.append(
                DayFigures(
                    day=day,
                    grid_import=imported,
                    grid_export=exported,
                    energy_cost=_delta(running, previous, "charges"),
                    export_credit=-_delta(running, previous, "credits"),
                    amount_due=_cash_due(running, opening) - _cash_due(previous, opening),
                )
            )
        # Advanced whether or not the day was published, so a refused day's
        # charges stay unpublished rather than landing on the next one.
        previous = running
    return days


def _delta(running: Bill, previous: Bill | None, part: str) -> float:
    def read(bill: Bill | None) -> float:
        if bill is None:
            return 0.0
        if part == "charges":
            return bill.import_charges
        return bill.export_credits

    return read(running) - read(previous)


def _cash_due(bill: Bill | None, opening: CreditBalances | None) -> float:
    """What ``bill`` leaves owed, or nothing at all for the day before day one."""
    return 0.0 if bill is None else apply_credits(bill, opening).cash_due


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

    walks: list[CycleWalk] = []
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
        walk = walk_cycle(profile, readings, cycle)
        if walk.reason or walk.bill is None:
            # `reason` already names a period, so quote only its explanation.
            detail = walk.reason.split(": ", 1)[-1]
            result.skipped.append(f"{cycle.start}..{cycle.end}: {detail}")
            continue
        walks.append(walk)
        result.bills.append(walk.bill)
        result.unpriced.extend(walk.unmetered)
        for warning in (*walk.warnings, *coverage_warnings(_within(readings, cycle), cycle)):
            if warning not in result.warnings:
                result.warnings.append(warning)

    if not walks:
        return result

    # Only now can a day be told what it owes. What is owed depends on the bank
    # a cycle opens with, and a bank does not simply accumulate: an annual
    # true-up claws back credit already paid out as Net Surplus Compensation, so
    # every cycle after an anniversary opens lower than a straight fold would
    # say. Chaining `apply_credits` cycle to cycle -- which is what this did --
    # skips that entirely, and published a history the live entities disagreed
    # with by $572 on a run crossing one anniversary.
    result.lifetime = run_lifetime(
        result.bills,
        pto_date=profile.pto_date,
        is_cca=_is_cca(profile, result.bills[-1].period.end),
    )
    result.warnings.extend(_bank_warnings(profile, result, opens, start_day))
    for walk in walks:
        result.days.extend(decompose(walk, result.lifetime.opening_for(walk.cycle)))
    result.days.sort(key=lambda figures: figures.day)
    return result


def _is_cca(profile: AccountProfile, on: date) -> bool:
    """Whether a Community Choice Aggregator supplies generation.

    Which decides whether an annual cash-out is one of the settlements to
    apply. A bundled account has no CCA to settle with.
    """
    from tariffkit.models import Supplier

    try:
        config = profile.config_at(datetime(on.year, on.month, on.day, 12, tzinfo=PACIFIC))
    except TariffKitError, ValueError:
        return False
    return config.supplier is Supplier.CCA


def _bank_warnings(
    profile: AccountProfile, result: BackfillResult, opens: date, start_day: int
) -> list[str]:
    """Why the bank this run folds from may not be the account's real one.

    A backfill opens the bank at zero, which is true only when it starts at the
    cycle holding Permission To Operate -- before that nothing is compensated,
    so there is nothing to carry. Started anywhere later, every credit earned in
    between is missing, and every cycle in the window is overstated by whatever
    that bank would have offset. Measured at $677 over three cycles on a window
    beginning nine months after PTO, reported as complete with nothing skipped.
    """
    pto = profile.pto_date
    if pto is None:
        return []
    begins = resolve_cycle(pto, start_day, statement_periods(profile)).start
    if opens <= begins:
        return []
    return [
        f"this run starts at {opens}, after the cycle containing Permission To Operate "
        f"({begins}), so it opens the export credit bank at zero. Any credit earned "
        f"between those dates is missing, and every amount due here is overstated by "
        f"whatever it would have offset. Backfill from {begins} for a bank that carries"
    ]


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

    # Every day in the span, including the ones that could not be priced.
    # Writing external statistics only inserts or updates the rows it is handed;
    # it never deletes. A rerun that publishes fewer days -- which is exactly
    # what refusing a reconstructed day produces -- would otherwise leave that
    # day behind at the price a previous run gave it, and the following day
    # would absorb the whole correction as a zero. A day nothing can be said
    # about is written as zero rather than left holding a stale figure.
    priced = {figures.day: figures for figures in result.days}
    span = sorted({*priced, *result.unpriced})
    running = base
    rows: list[StatisticData] = []
    for day in span:
        figures = priced.get(day)
        value = figures.value(series.slug) if figures is not None else 0.0
        running += value
        rows.append(
            StatisticData(
                start=datetime(day.year, day.month, day.day, tzinfo=PACIFIC),
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
        # Nothing could be priced, so this run knows nothing about the window.
        # Publishing anyway would write a zero row for every unpriced day --
        # `statistics_for` emits one per day by design -- straight over history
        # a previous run got right, and external statistics are never deleted.
        # Leaving the old figures standing is the lesser wrong.
        return
    published = sorted({figures.day for figures in result.days}.union(result.unpriced))
    # Anchored at the first row actually written, not the first *priced* day.
    # `statistics_for` emits a zero row for every day in priced + unpriced, so
    # when a rerun's leading day flips to refused the span opens earlier than
    # `days[0]`. Reading the base from the later date meant the refused day's
    # own previous contribution was already inside the base, and was then
    # re-added under a row whose state reads 0.0 -- so the day still charged
    # its old figure and every later day carried it, permanently. External
    # statistics are never deleted, so no rerun with the same start undid it.
    bases = await async_base_sums(hass, profile_name, published[0])
    for series in SERIES:
        statistic_id = series.statistic_id(profile_name)
        rows = statistics_for(result, series, bases.get(statistic_id, 0.0))
        if not rows:
            continue
        async_add_external_statistics(hass, series.metadata(profile_name), rows)
        _LOGGER.debug("wrote %d rows to %s", len(rows), statistic_id)
