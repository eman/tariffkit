"""EV2-A, reconciled against a real April 2026 statement.

PG&E delivery + MCE generation, 2026-04-01..04-29, 29 billing days, 376.180 kWh
imported and none exported. Every rate asserted here was printed on that
statement, so a regression shows up as a mismatch against a real bill rather
than against an earlier version of ourselves.

    peak       1.084 kWh @ $0.41099 = $0.45
    part-peak  0.930 kWh @ $0.39428 = $0.37
    off-peak 374.166 kWh @ $0.22558 = $84.40
    Base Services Charge  29 days @ $0.79343 = $23.01

This is the first winter cycle reconciled on any schedule: the vendored E-ELEC
and E-TOU-C winter rates have no statement behind them yet.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tariffkit import Config, RateEngine, Supplier
from tariffkit.billing.engine import BillEngine
from tariffkit.billing.models import BillingPeriod, IntervalReading
from tariffkit.config import CcaConfig
from tariffkit.timeutil import PACIFIC

DAYS = 29
PERIOD = BillingPeriod(date(2026, 4, 1), date(2026, 4, 29))
USAGE = {"peak": 1.084, "part_peak": 0.930, "off_peak": 374.166}
TOTAL_KWH = 376.180
#: One representative hour per TOU period, inside the billed cycle.
HOURS = {"peak": 17, "part_peak": 15, "off_peak": 12}


def config() -> Config:
    return Config(
        tariff="EV2-A",
        supplier=Supplier.CCA,
        cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2011),
    )


def at(hour: int) -> datetime:
    return datetime(2026, 4, 15, hour, tzinfo=PACIFIC)


def readings() -> list[IntervalReading]:
    return [
        IntervalReading(datetime(2026, 4, 15, HOURS[period], tzinfo=PACIFIC), imported=kwh)
        for period, kwh in USAGE.items()
    ]


class TestBundledDeliveryRates:
    """PG&E's side. These are total rates, printed on the statement."""

    @pytest.mark.parametrize(
        ("period", "billed"),
        [("peak", 0.41099), ("part_peak", 0.39428), ("off_peak", 0.22558)],
    )
    def test_total_rate_matches_the_statement(self, period: str, billed: float) -> None:
        price = RateEngine(Config(tariff="EV2-A")).price_at(at(HOURS[period])).import_price
        assert price.total == pytest.approx(billed)
        assert str(price.period) == period

    def test_the_cycle_is_priced_as_winter(self) -> None:
        """April is winter on EV2-A; summer starts June 1."""
        assert str(RateEngine(Config(tariff="EV2-A")).price_at(at(12)).import_price.season) == (
            "winter"
        )

    def test_base_services_charge(self) -> None:
        charge = RateEngine(Config(tariff="EV2-A")).daily_fixed_charge(at(12))
        assert charge == pytest.approx(0.79343)
        assert round(DAYS * charge, 2) == 23.01

    def test_no_baseline_on_this_schedule(self) -> None:
        """The statement shows no Baseline Territory line, unlike E-TOU-C."""
        assert RateEngine(Config(tariff="EV2-A")).price_at(at(12)).import_price.baseline_credit == 0


class TestMceGenerationRates:
    @pytest.mark.parametrize(
        ("period", "billed"),
        [("peak", 0.13143), ("part_peak", 0.11894), ("off_peak", 0.09546)],
    )
    def test_generation_matches_the_statement(self, period: str, billed: float) -> None:
        components = RateEngine(config()).price_at(at(HOURS[period])).import_price.components
        assert components["cca_generation"] == pytest.approx(billed)

    def test_cost_relief_credit(self) -> None:
        components = RateEngine(config()).price_at(at(12)).import_price.components
        assert components["cca_cost_relief_credit"] == pytest.approx(-0.00620)

    def test_these_are_not_the_eelec_rates(self) -> None:
        """The bug this schedule-keyed card exists to prevent.

        MCE's E-ELEC winter off-peak is 0.06754; billing an EV2-A customer at it
        would understate generation by nearly a third.
        """
        components = RateEngine(config()).price_at(at(12)).import_price.components
        assert components["cca_generation"] != pytest.approx(0.06754)


class TestFlatRiders:
    """Reconciled against the statement's own "Electric Charges Breakdown"."""

    @pytest.mark.parametrize(
        ("component", "billed"),
        [
            ("wildfire_fund_charge", 2.22),
            ("recovery_bond_charge", 3.22),
            ("wildfire_hardening", 1.47),
            ("competition_transition_charges", 0.10),
            ("energy_cost_recovery", 0.01),
            ("nuclear_decommissioning", -0.01),
            ("pcia", 13.14),
            ("franchise_fee_surcharge", 0.23),
        ],
    )
    def test_rider_reproduces_the_billed_dollars(self, component: str, billed: float) -> None:
        rate = RateEngine(config()).price_at(at(12)).import_price.components[component]
        assert round(rate * TOTAL_KWH, 2) == pytest.approx(billed, abs=0.011)

    def test_transmission_is_billed_as_three_combined_components(self) -> None:
        """The sheet says transmission, its adjustments and reliability services
        are combined for presentation, and the statement shows one $19.19 line."""
        components = RateEngine(config()).price_at(at(12)).import_price.components
        combined = sum(
            components[name]
            for name in ("transmission", "transmission_rate_adjustments", "reliability_services")
        )
        assert round(combined * TOTAL_KWH, 2) == pytest.approx(19.19, abs=0.011)


class TestBillReconciliation:
    def test_energy_charges(self) -> None:
        """PG&E's three energy lines sum to $85.22."""
        bundled = RateEngine(Config(tariff="EV2-A"))
        total = sum(
            kwh * bundled.price_at(at(HOURS[period])).import_price.total
            for period, kwh in USAGE.items()
        )
        assert total == pytest.approx(85.22, abs=0.01)

    def test_mce_net_charges(self) -> None:
        """MCE's page: gross generation less the cost relief credit, $33.64."""
        engine = RateEngine(config())
        gross = sum(
            kwh * engine.price_at(at(HOURS[period])).import_price.components["cca_generation"]
            for period, kwh in USAGE.items()
        )
        relief = TOTAL_KWH * 0.00620
        assert gross - relief == pytest.approx(33.64, abs=0.01)

    def test_usage_buckets_match_the_statement(self) -> None:
        bill = BillEngine(RateEngine(config())).compute(readings(), PERIOD, check=False)
        by_period = {str(b.period): b for b in bill.buckets}
        for period, kwh in USAGE.items():
            assert by_period[period].imported == pytest.approx(kwh)
        assert bill.imported_kwh == pytest.approx(TOTAL_KWH)

    def test_fixed_charge_over_the_cycle(self) -> None:
        bill = BillEngine(RateEngine(config())).compute(readings(), PERIOD, check=False)
        assert bill.fixed_components["base_services_charge"] == pytest.approx(23.01, abs=0.01)

    def test_the_cycle_prices_at_all(self) -> None:
        """Regression on the snapshot's effective date.

        The rates were transcribed from a sheet carrying Advice 7921-E effective
        2026-06-01, but this cycle was billed at exactly them two months earlier.
        Dating the snapshot from the advice letter made the whole statement
        unpriceable.
        """
        assert RateEngine(config()).price_at(datetime(2026, 4, 1, 0, tzinfo=PACIFIC))
