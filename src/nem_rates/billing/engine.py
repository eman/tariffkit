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
from datetime import datetime, timedelta

from ..engine import RateEngine
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
        import_components: dict[str, float] = {}
        export_components: dict[str, float] = {}
        complete = True

        for reading in in_period:
            # Price at the hour containing the interval: rates change hourly,
            # data may be finer.
            moment = hour_floor(to_pacific(reading.start))
            point = self.rates.price_at(moment)
            import_price = point.import_price
            export_price = point.export_price

            if not (import_price.complete and export_price.complete and export_price.exact):
                complete = False

            key = (import_price.season, import_price.period)
            bucket = buckets.setdefault(key, _Accumulator(*key))

            if reading.imported:
                bucket.imported += reading.imported
                bucket.import_charge += reading.imported * import_price.total
                _add_scaled(import_components, import_price.components, reading.imported)

            if reading.exported:
                bucket.exported += reading.exported
                # Credits are negative so the bill sums directly.
                bucket.export_credit -= reading.exported * export_price.total
                _add_scaled(export_components, export_price.components, -reading.exported)

        fixed_components = self._fixed_charges(period)

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
