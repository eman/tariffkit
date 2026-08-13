"""Compute a bill from interval readings.

Pure: takes metered energy and a rate engine, returns charges. Nothing here
knows where the readings came from.

Scope note -- this computes a single period's charges. It deliberately does not
model export-credit *balances*: carryover between months, the annual true-up, or
Net Surplus Compensation. Those are stateful across a whole program year and
belong in a ledger built on top of this.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise

from ..cca import load_rate_card
from ..config import Config
from ..engine import RateEngine
from ..errors import DataError
from ..models import ImportPrice, Season, TouPeriod
from ..timeutil import PACIFIC, hour_floor, to_pacific
from .models import Bill, BillingPeriod, IntervalReading, UsageBucket
from .netting import check_coverage

#: Components billed on gross import regardless of what a site exported. Kept
#: for reference: with per-interval netting these already apply to every
#: imported kWh, so no separate handling is needed. It becomes load-bearing only
#: if a monthly-netting mode is ever added.
NON_BYPASSABLE = (
    "public_purpose_programs",
    "wildfire_fund_charge",
    "competition_transition_charges",
    "nuclear_decommissioning",
    "energy_cost_recovery",
)


class BillEngine:
    """Turns interval readings into a decomposed bill."""

    def __init__(self, rates: RateEngine | None = None) -> None:
        self.rates = rates or RateEngine()

    def _compensated(self, moment: datetime) -> bool:
        """Whether an export at ``moment`` earns anything.

        Net Billing compensation runs from Permission To Operate. Energy leaving
        the house before then is real -- the meter records it -- but the tariff
        grants nothing for it, so crediting it would invent money.
        """
        pto = self.rates.config.pto_date
        return pto is None or moment.date() >= pto

    def compute(
        self,
        readings: Iterable[IntervalReading],
        period: BillingPeriod | None = None,
        *,
        check: bool = True,
    ) -> Bill:
        """Price ``readings`` over ``period``.

        ``period`` defaults to the span of the readings themselves. Readings
        outside it are ignored, so a year of data can be billed one cycle at a
        time without slicing it first.
        """
        readings = list(readings)
        if period is None:
            period = BillingPeriod.from_readings(readings)

        in_period = [r for r in readings if period.contains(r.start)]
        warnings = list(check_coverage(in_period, period)) if check else []

        buckets: dict[tuple[Season, TouPeriod], _Accumulator] = {}
        uncompensated = 0.0
        import_components: dict[str, float] = {}
        export_components: dict[str, float] = {}
        complete = True

        for reading in in_period:
            # Price at the hour containing the interval: rates change hourly,
            # data may be finer.
            moment = hour_floor(to_pacific(reading.start))
            import_price = self.rates.tariff.price_at(moment)

            # Only ask what an export was worth when it could earn anything.
            #
            # Export compensation starts at Permission To Operate: before it
            # there is no Net Billing arrangement, whatever the meter saw. The
            # two data sources disagree about that on purpose -- PG&E's own
            # export reports zero exported kWh for the December 2025 cycle
            # because there was no export channel to meter, while the Rainforest
            # counter behind it recorded real energy leaving the house. Pricing
            # the counter's view would invent credits the tariff does not grant.
            export_price = None
            if reading.exported and self._compensated(moment):
                export_price = self.rates.export_rates.price_at(moment)
            elif reading.exported:
                uncompensated += reading.exported

            if not import_price.complete:
                complete = False
            if export_price is not None and not (export_price.complete and export_price.exact):
                complete = False

            key = (import_price.season, import_price.period)
            bucket = buckets.setdefault(key, _Accumulator(*key))

            if reading.imported:
                bucket.imported += reading.imported
                bucket.import_charge += reading.imported * import_price.total
                _add_scaled(import_components, import_price.components, reading.imported)

            if reading.exported and export_price is not None:
                bucket.exported += reading.exported
                # Credits are negative so the bill sums directly.
                bucket.export_credit -= reading.exported * export_price.total
                _add_scaled(export_components, export_price.components, -reading.exported)

        fixed_components = self._fixed_charges(period)
        tax, untaxed_days = self._energy_surcharge(in_period, period)
        if tax:
            import_components["energy_commission_tax"] = tax
        if untaxed_days:
            complete = False
            warnings.append(
                f"no energy surcharge vintage covers {len(untaxed_days)} day(s) "
                f"({untaxed_days[0]} to {untaxed_days[-1]}); those days carry no tax, "
                f"so the total is understated. Run `nem-rates regen tax`."
            )

        stale = self._stale_rate_card(period)
        if stale:
            warnings.append(stale)

        credit = self._baseline_credit(in_period, period)
        if credit:
            import_components["baseline_credit"] = credit

        if uncompensated:
            warnings.append(
                f"{uncompensated:.1f} kWh exported before the Permission To Operate date "
                f"({self.rates.config.pto_date}); Net Billing compensation starts at PTO, "
                f"so it earns nothing and is not credited here"
            )

        return Bill(
            period=period,
            buckets=tuple(
                b.finish() for b in sorted(buckets.values(), key=lambda a: (a.season, a.period))
            ),
            import_components=import_components,
            export_components=export_components,
            fixed_components=fixed_components,
            warnings=tuple(warnings),
            # Pricing confidence only. Coverage problems travel separately in
            # `warnings`: they say the meter data is patchy, not that the rates
            # applied to it are uncertain, and folding them together made a bill
            # that reconciles to a statement still describe itself as an
            # estimate. Callers wanting "trust this total" should check both.
            complete=complete,
        )

    def _energy_surcharge(
        self, readings: Sequence[IntervalReading], period: BillingPeriod
    ) -> tuple[float, list[date]]:
        """California's Energy Resources Surcharge on energy consumed.

        A state tax rather than a utility tariff, so it is charged whoever
        supplies the generation. A statement prints it as "Energy Commission
        Tax", and when a CCA supplies generation it prints on *their* page --
        which is how it went unmodelled while every line on the utility's pages
        reconciled.

        Rated per kilowatt-hour imported, by the vintage in force on each day, so
        a cycle spanning a January rate change is charged correctly.

        Returns the charge and the days no vintage covered. Those days are not
        charged, and the caller says so and marks the bill incomplete: a bill
        that quietly omits a tax is the plausible-but-wrong kind, which is worse
        than one that refuses to claim it is finished.
        """
        from ..data import versioned

        total = 0.0
        remaining: dict[date, float] = {}
        for reading in readings:
            remaining[to_pacific(reading.start).date()] = (
                remaining.get(to_pacific(reading.start).date(), 0.0) + reading.imported
            )
        uncovered: list[date] = []
        for day, imported in sorted(remaining.items()):
            if not imported:
                continue
            try:
                rate = float(versioned.load("tax/ca_energy_resources", day).raw["rate"])
            except DataError:
                # The rest of the bill is still worth producing, so this is not
                # fatal -- but it is not silent either.
                uncovered.append(day)
                continue
            total += imported * rate
        return total, uncovered

    #: How far a CCA rate card may predate a cycle before it is worth saying so.
    #: A CCA reprices at least annually, so a card more than a year older than
    #: the energy it is pricing is being *borrowed*, not merely still in force.
    STALE_CARD = timedelta(days=400)

    def _stale_rate_card(self, period: BillingPeriod) -> str:
        """Whether the CCA generation was priced from a much older rate card.

        `versioned.load` takes the latest vintage on or before the date, which
        is right for a tariff -- a rate stays in force until superseded. It is
        indistinguishable, though, from "nobody vendored the vintage that was
        actually in force", and the two are worlds apart: the first is correct,
        the second silently prices 2025 energy at 2023 rates.

        This cannot tell them apart either. It can say how old the card is and
        let the reader judge, which is the whole difference between a number
        that is wrong and a number that is wrong and says nothing.
        """
        cca = self.rates.config.cca
        if cca is None or cca.rate_card is None or cca.generation_rates:
            return ""
        try:
            card = load_rate_card(cca.rate_card, period.end)
        except DataError:
            return ""
        age = period.end - card.effective
        if age <= self.STALE_CARD:
            return ""
        return (
            f"{cca.rate_card.upper()} generation priced from the rate card effective "
            f"{card.effective}, {age.days} days before this cycle ended. Either the "
            f"provider did not reprice in between, or the vintage that applied was "
            f"never vendored -- and nothing here can tell those apart"
        )

    def _baseline_credit(self, readings: Sequence[IntervalReading], period: BillingPeriod) -> float:
        """Credit on imports falling within the cycle's baseline allowance.

        Only schedules with a baseline produce one, and only when a territory is
        configured. It lands here rather than in the marginal price because
        eligibility depends on cumulative usage over the cycle, which
        ``price_at`` cannot see.

        Both the allowance and the credit rate are daily quantities, and both
        can change inside one cycle -- the allowance at the season boundary, the
        rate whenever a new tariff vintage takes force. So this walks the days
        and credits each one at its own rate rather than reading a rate once.

        A statement spanning a rate change prints exactly that: the December
        2025 cycle shows 19.40 kWh at $0.10084 for its two December days and
        281.30 kWh at $0.09566 for its twenty-nine January ones. Reading one
        rate for the cycle applied December's to all 300.70 kWh and overstated
        the credit by $1.45.

        The credit is identical in every TOU period, so how PG&E allocates
        baseline usage across periods moves the printed lines but not this total.
        """
        tariff = self.rates.tariff
        remaining = sum(r.imported for r in readings)
        if remaining <= 0:
            return 0.0

        credit = 0.0
        for offset in range(period.days):
            day = period.start + timedelta(days=offset)
            noon = datetime(day.year, day.month, day.day, 12, tzinfo=PACIFIC)
            allowance = tariff.baseline_allowance(noon)
            if not allowance:
                continue
            rate = tariff.price_at(noon).baseline_credit
            if not rate:
                continue
            # Imports are credited against the allowance in day order, so a
            # cycle that used less than its allowance is capped rather than
            # credited for energy it never took.
            within = min(allowance, remaining)
            credit -= within * rate
            remaining -= within
            if remaining <= 0:
                break
        return credit

    def _fixed_charges(self, period: BillingPeriod) -> dict[str, float]:
        """Charges billed per day rather than per kWh.

        Priced day by day, because the utility prorates and a daily charge can
        begin mid-cycle. AB 205's Base Services Charge began on 2026-03-01, and
        the January-to-March cycle that spans it is billed 30 days at nothing
        and 2 days at the new rate. Pricing the whole cycle from the tariff in
        force on its first day charges nothing at all for those two days, which
        is a real dollar and change on a statement that otherwise reconciles to
        the cent -- small enough to look like rounding, which is what makes it
        worth getting right rather than tolerating.
        """
        total = 0.0
        for offset in range(period.days):
            day = period.start + timedelta(days=offset)
            moment = datetime(day.year, day.month, day.day, 12, tzinfo=PACIFIC)
            total += self.rates.tariff.daily_fixed_charge(moment)
        return {"base_services_charge": total}

    def marginal_rates(
        self, readings: Sequence[IntervalReading]
    ) -> dict[tuple[Season, TouPeriod], ImportPrice]:
        """The distinct import prices that applied across ``readings``.

        Useful for showing which rate produced a bucket without re-deriving it.
        """
        seen: dict[tuple[Season, TouPeriod], ImportPrice] = {}
        for reading in readings:
            price = self.rates.price_at(reading.start).import_price
            seen.setdefault((price.season, price.period), price)
        return seen


def _add_scaled(target: dict[str, float], source: dict[str, float], kwh: float) -> None:
    for name, rate in source.items():
        target[name] = target.get(name, 0.0) + rate * kwh


class _Accumulator:
    __slots__ = ("export_credit", "exported", "import_charge", "imported", "period", "season")

    def __init__(self, season: Season, period: TouPeriod) -> None:
        self.season = season
        self.period = period
        self.imported = 0.0
        self.exported = 0.0
        self.import_charge = 0.0
        self.export_credit = 0.0

    def finish(self) -> UsageBucket:
        return UsageBucket(
            season=self.season,
            period=self.period,
            imported=self.imported,
            exported=self.exported,
            import_charge=self.import_charge,
            export_credit=self.export_credit,
        )


def hourly(readings: Iterable[IntervalReading]) -> list[IntervalReading]:
    """Collapse sub-hourly readings into whole hours.

    Netting granularity matters: summing 15-minute imports and exports into an
    hour before netting gives a different answer than netting each quarter hour.
    This helper preserves the finer netting by summing each side separately, so
    it only changes how charges are grouped, never what they total.
    """
    merged: dict[datetime, list[float]] = {}
    for reading in readings:
        key = hour_floor(to_pacific(reading.start))
        slot = merged.setdefault(key, [0.0, 0.0])
        slot[0] += reading.imported
        slot[1] += reading.exported
    return [
        IntervalReading(start, imported=imp, exported=exp, duration=timedelta(hours=1))
        for start, (imp, exp) in sorted(merged.items())
    ]


def _ordered_segments(segments: Sequence[Segment]) -> list[Segment]:
    if not segments:
        raise DataError("a bill needs at least one segment")
    ordered = sorted(segments, key=lambda s: s.period.start)
    for earlier, later in pairwise(ordered):
        if later.period.start <= earlier.period.end:
            raise DataError(
                f"segments overlap: {earlier.period.start}..{earlier.period.end} and "
                f"{later.period.start}..{later.period.end}. Overlapping segments would "
                f"price the same day twice"
            )
    return ordered


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch of a cycle, priced under its own configuration.

    A cycle is not always billed under a single tariff. When an account changes
    schedule mid-cycle -- or interconnects solar, which closes one service
    agreement and opens another -- the utility prices each stretch separately
    and prints them as separate blocks on one statement.
    """

    config: Config
    period: BillingPeriod


def price_segments(
    segments: Sequence[Segment],
    readings: Iterable[IntervalReading],
    *,
    check: bool = True,
) -> list[Bill]:
    """One bill per segment, unmerged.

    Kept separate from :func:`compute_segments` because export credits do not
    cross a service agreement. A cycle where solar was interconnected carries a
    closed agreement and a new one, and the utility applies the new agreement's
    export credits only against its own charges -- on 2026-07-07 it spends 2.18
    against the Solar Billing Plan's charges and nothing against the closed
    agreement's 0.94, which predates Permission To Operate and has no export
    arrangement at all. A ledger run over the merged bill spends them against
    both and overstates what was applied.
    """
    ordered = _ordered_segments(segments)
    readings = list(readings)
    return [
        BillEngine(RateEngine(segment.config)).compute(readings, segment.period, check=check)
        for segment in ordered
    ]


def compute_segments(
    segments: Sequence[Segment],
    readings: Iterable[IntervalReading],
    *,
    check: bool = True,
) -> Bill:
    """Price one cycle that more than one configuration governs.

    Each segment is priced by its own engine over its own dates and the results
    are added, because that is what the utility does: a mid-cycle schedule
    change produces two blocks on one statement, not a blended rate.

    Refusing this case and demanding a single ``Config`` was the wrong shape.
    The months worth checking most are exactly the ones where something changed,
    and a harness that skips them checks only the quiet months.
    """
    ordered = _ordered_segments(segments)
    readings = list(readings)
    whole = BillingPeriod(ordered[0].period.start, ordered[-1].period.end)

    imports: dict[str, float] = {}
    exports: dict[str, float] = {}
    fixed: dict[str, float] = {}
    buckets: dict[tuple[Season, TouPeriod], UsageBucket] = {}
    warnings: list[str] = []
    complete = True

    for segment, part in zip(ordered, price_segments(ordered, readings, check=check), strict=True):
        for target, source in (
            (imports, part.import_components),
            (exports, part.export_components),
            (fixed, part.fixed_components),
        ):
            for key, value in source.items():
                target[key] = target.get(key, 0.0) + value

        for bucket in part.buckets:
            slot = (bucket.season, bucket.period)
            running = buckets.get(slot)
            buckets[slot] = UsageBucket(
                season=bucket.season,
                period=bucket.period,
                imported=(running.imported if running else 0.0) + bucket.imported,
                exported=(running.exported if running else 0.0) + bucket.exported,
                import_charge=(running.import_charge if running else 0.0) + bucket.import_charge,
                export_credit=(running.export_credit if running else 0.0) + bucket.export_credit,
            )

        # Attributed, because "no tax vintage covers 3 days" is a different
        # problem depending on which tariff was in force when it happened.
        warnings.extend(
            f"{segment.period.start}..{segment.period.end} ({segment.config.tariff}): {warning}"
            for warning in part.warnings
        )
        complete = complete and part.complete

    return Bill(
        period=whole,
        buckets=tuple(buckets.values()),
        import_components=imports,
        export_components=exports,
        fixed_components=fixed,
        warnings=tuple(warnings),
        complete=complete,
    )
