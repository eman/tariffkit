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
from datetime import date, datetime, timedelta

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
        tax = self._energy_surcharge(in_period, period)
        if tax:
            import_components["energy_commission_tax"] = tax

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
    ) -> float:
        """California's Energy Resources Surcharge on energy consumed.

        A state tax rather than a utility tariff, so it is charged whoever
        supplies the generation. A statement prints it as "Energy Commission
        Tax", and when a CCA supplies generation it prints on *their* page --
        which is how it went unmodelled while every line on the utility's pages
        reconciled.

        Rated per kilowatt-hour imported, by the vintage in force on each day, so
        a cycle spanning a January rate change is charged correctly.
        """
        from ..data import versioned

        total = 0.0
        remaining: dict[date, float] = {}
        for reading in readings:
            remaining[to_pacific(reading.start).date()] = (
                remaining.get(to_pacific(reading.start).date(), 0.0) + reading.imported
            )
        for day, imported in remaining.items():
            if not imported:
                continue
            try:
                rate = float(versioned.load("tax/ca_energy_resources", day).raw["rate"])
            except DataError:
                # No vintage covers this day. The surcharge is small and the
                # rest of the bill is still worth producing, so this is left off
                # rather than made fatal -- and the coverage check already tells
                # the reader which period is being priced.
                continue
            total += imported * rate
        return total

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

        Priced from the tariff in force at the start of the cycle. A rate change
        mid-cycle is not prorated; PG&E does prorate, so a cycle spanning one
        would be slightly off.
        """
        moment = datetime(
            period.start.year, period.start.month, period.start.day, 12, tzinfo=PACIFIC
        )
        daily = self.rates.tariff.daily_fixed_charge(moment)
        return {"base_services_charge": daily * period.days}

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
