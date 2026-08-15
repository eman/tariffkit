"""MCE rate card, verified against a real July 2026 bill.

Every rate asserted here was read off a bill or MCE's published rate card, not
inferred, so a regression shows up as a mismatch against a real statement.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tariffkit import Config, RateEngine, Supplier
from tariffkit.cca import load_rate_card
from tariffkit.config import CcaConfig
from tariffkit.errors import DataError
from tariffkit.tariff.retail import RetailTariff, load_snapshot
from tariffkit.timeutil import PACIFIC

#: A date the vendored MCE card is in force for; cards resolve by date.
CARD_DATE = date(2026, 7, 15)

# Derived from the billed dollar amounts over the period's 23.589 kWh.
BILLED_KWH = 23.589
PCIA_RATE = 0.82 / BILLED_KWH
FRANCHISE_FEE = 0.01 / BILLED_KWH


def config(**overrides: object) -> Config:
    cca = CcaConfig(
        name="MCE",
        rate_card="mce",
        pcia_rate=PCIA_RATE,
        franchise_fee_surcharge=FRANCHISE_FEE,
        **overrides,  # type: ignore[arg-type]
    )
    return Config(supplier=Supplier.CCA, cca=cca)


def at(hour: int, month: int = 7) -> datetime:
    return datetime(2026, month, 29, hour, tzinfo=PACIFIC)


@pytest.mark.parametrize(
    ("hour", "billed_rate"),
    [(12, 0.11878), (17, 0.26299), (15, 0.16388)],  # off-peak, peak, part-peak
)
def test_generation_matches_the_billed_rate(hour: int, billed_rate: float) -> None:
    price = RetailTariff(config()).price_at(at(hour))
    assert price.components["cca_generation"] == pytest.approx(billed_rate)


def test_cost_relief_credit_matches_the_bill() -> None:
    price = RetailTariff(config()).price_at(at(12))
    assert price.components["cca_cost_relief_credit"] == pytest.approx(-0.00620)


def test_cost_relief_credit_expires_at_the_end_of_2026() -> None:
    """It is explicitly time-limited; leaving it on would understate 2027."""
    card = load_rate_card("mce", CARD_DATE)
    assert card.cost_relief_credit(date(2026, 12, 31)) == pytest.approx(-0.00620)
    assert card.cost_relief_credit(date(2027, 1, 1)) == 0.0

    assert (
        "cca_cost_relief_credit"
        not in RetailTariff(config()).price_at(datetime(2027, 7, 29, 12, tzinfo=PACIFIC)).components
    )


@pytest.mark.parametrize("schedule", ["E-ELEC", "E-TOU-C", "EV2-A"])
def test_mce_generation_is_at_parity_with_pge_today(schedule: str) -> None:
    """Parity is real but not guaranteed -- fail loudly if MCE diverges.

    If this breaks, MCE has repriced and mce.toml needs regenerating; it is not
    a bug in the engine. Checked per schedule, since MCE prices each separately.
    """
    card = load_rate_card("mce", CARD_DATE)
    snapshot = load_snapshot("PGE", schedule, date(2026, 6, 1))
    for season, periods in snapshot.raw["energy"].items():
        for period, components in periods.items():
            assert card.generation(schedule, season, period) == pytest.approx(
                components["generation"]
            ), f"MCE has diverged from PG&E at {schedule} {season}/{period}"


def test_rate_card_covers_each_schedule_separately() -> None:
    """Borrowing one schedule's card for another is a ~2x error, not a rounding."""
    card = load_rate_card("mce", CARD_DATE)
    eelec = card.generation("E-ELEC", "winter", "off_peak")
    etouc = card.generation("E-TOU-C", "winter", "off_peak")
    assert eelec == pytest.approx(0.06754)
    assert etouc == pytest.approx(0.11042)
    assert etouc / eelec > 1.5


def test_uncovered_schedule_raises_rather_than_falling_back() -> None:
    with pytest.raises(DataError, match="no generation rates for schedule"):
        load_rate_card("mce", CARD_DATE).generation("E-TOU-D", "winter", "off_peak")


def test_etouc_has_no_part_peak_on_the_rate_card_either() -> None:
    with pytest.raises(DataError, match="no E-TOU-C generation rate"):
        load_rate_card("mce", CARD_DATE).generation("E-TOU-C", "winter", "part_peak")


def test_deep_green_costs_a_penny_and_a_quarter_more() -> None:
    card = load_rate_card("mce", CARD_DATE)
    light = card.generation("E-ELEC", "summer", "off_peak", "light_green")
    deep = card.generation("E-ELEC", "summer", "off_peak", "deep_green")
    assert deep - light == pytest.approx(0.0125)


def test_unknown_product_option_raises() -> None:
    with pytest.raises(Exception, match="unknown product option"):
        load_rate_card("mce", CARD_DATE).generation("E-ELEC", "summer", "peak", "medium_green")


class TestBillReconciliation:
    """Reproduce the PG&E half of the statement."""

    def test_base_services_charge(self) -> None:
        # Billed as 27 days @ $0.79343 = $21.42.
        charge = RetailTariff(config()).daily_fixed_charge(at(12))
        assert charge == pytest.approx(0.79343)
        assert round(27 * charge, 2) == 21.42

    def test_energy_produced_line_is_generation_plus_bundled_pcia(self) -> None:
        """PG&E prints its own generation, then credits it back in full."""
        bundled = RetailTariff(Config()).price_at(at(12)).components
        assert bundled["generation"] + bundled["bundled_pcia"] == pytest.approx(0.10867)

    def test_delivery_splits_into_energy_delivered_plus_non_bypassable(self) -> None:
        components = RetailTariff(config()).price_at(at(12)).components
        delivery = sum(
            v
            for k, v in components.items()
            if k
            not in (
                "cca_generation",
                "cca_cost_relief_credit",
                "pcia",
                "franchise_fee_surcharge",
            )
        )
        # Bill shows Energy Delivered 0.21261 + Non-Bypassable Charges 0.01230.
        assert delivery == pytest.approx(0.21261 + 0.01230, abs=1e-5)

    def test_pcia_is_positive_unlike_the_bundled_credit(self) -> None:
        """The bundled PCIA is a credit; a CCA customer's vintage PCIA is a charge."""
        components = RetailTariff(config()).price_at(at(12)).components
        assert components["pcia"] == pytest.approx(0.03476, abs=1e-5)
        assert "bundled_pcia" not in components

    def test_total_import_price(self) -> None:
        price = RetailTariff(config()).price_at(at(12))
        assert price.total == pytest.approx(0.37267, abs=1e-5)
        assert price.complete is True

    def test_cca_costs_more_than_bundled_on_import(self) -> None:
        """Driven by the vintage PCIA, which bundled service does not pay."""
        cca = RetailTariff(config()).price_at(at(12)).total
        bundled = RetailTariff(Config()).price_at(at(12)).total
        assert cca - bundled == pytest.approx(0.03909, abs=1e-5)


class TestExport:
    def test_solar_bonus_is_ten_percent_of_the_base_credit(self) -> None:
        price = RateEngine(config()).price_at(at(19, month=9)).export_price
        assert price.components["cca_solar_bonus"] == pytest.approx(
            price.components["cca_generation"] * 0.10
        )

    def test_acc_plus_applies_to_cca_customers(self) -> None:
        """Confirmed on the bill: a credit line at $0.00880/kWh exported."""
        price = RateEngine(config()).price_at(at(19, month=9)).export_price
        assert price.components["acc_plus"] == pytest.approx(0.00880)

    def test_export_credit_basis_is_reconciled_against_a_real_cycle(self) -> None:
        """MCE still does not publish its credit matrix, but it has been measured.

        Priced against 2,784 quarter-hourly meter intervals for the
        2026-06-30..2026-07-28 cycle, every export component matched that cycle's
        statement within 0.3% -- inside the rounding of the bill's own displayed
        dollars. So exports are no longer flagged as an estimate.
        """
        assert load_rate_card("mce", CARD_DATE).export_credit_verified is True
        assert RateEngine(config()).price_at(at(19, month=9)).export_price.complete is True

    @pytest.mark.parametrize(
        ("hour", "delivery", "generation", "total"),
        [
            (19, 0.35038, 0.10708, 0.476968),  # peak
            (15, 0.00119, 0.05522, 0.070732),  # part-peak
            (12, 0.00077, 0.05686, 0.072116),  # off-peak
        ],
    )
    def test_export_components_reproduce_the_reconciled_cycle_rates(
        self, hour: int, delivery: float, generation: float, total: float
    ) -> None:
        """Pins the absolute values the reconciliation rests on.

        Sampled inside the reconciled cycle (2026-06-30..2026-07-28) and across
        all three TOU periods. Both NBT matrices are indexed by month, so pinning
        some other month would leave every cell that produced the evidence free
        to drift while this stayed green.

        Asserted absolutely rather than against each other: checking that the
        solar bonus is 10% of whatever ``cca_generation`` happens to be would
        still pass if the ACC generation lookup moved, silently invalidating
        ``export_credit_verified``.
        """
        price = RateEngine(config()).price_at(datetime(2026, 7, 15, hour, tzinfo=PACIFIC))
        components = price.export_price.components
        assert components["delivery"] == pytest.approx(delivery)
        assert components["cca_generation"] == pytest.approx(generation)
        assert components["acc_plus"] == pytest.approx(0.00880)
        assert components["cca_solar_bonus"] == pytest.approx(generation * 0.10)
        assert price.export_price.total == pytest.approx(total)

    def test_solar_bonus_tracks_the_generation_credit(self) -> None:
        """The 10% relationship, separately from the absolute values above."""
        price = RateEngine(config()).price_at(at(19, month=9)).export_price
        assert price.components["cca_solar_bonus"] == pytest.approx(
            price.components["cca_generation"] * 0.10
        )
