"""E-ELEC import pricing."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from nem_rates import Config, Season, Supplier, TouPeriod
from nem_rates.config import CcaConfig
from nem_rates.errors import ConfigError
from nem_rates.tariff.eelec import EelecTariff, load_snapshot
from nem_rates.timeutil import PACIFIC


@pytest.fixture
def tariff() -> EelecTariff:
    return EelecTariff(Config())


def pt(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=PACIFIC)


@pytest.mark.parametrize(
    ("season", "period", "published"),
    [
        ("summer", "peak", 0.55214),
        ("summer", "part_peak", 0.39026),
        ("summer", "off_peak", 0.33358),
        ("winter", "peak", 0.32063),
        ("winter", "part_peak", 0.29854),
        ("winter", "off_peak", 0.28468),
    ],
)
def test_components_sum_to_published_total(season: str, period: str, published: float) -> None:
    """The unbundled breakdown must reconstruct the published rate exactly.

    This is the check that catches a mistyped digit in the vendored tariff.
    """
    snapshot = load_snapshot("PGE", "E-ELEC", date(2026, 6, 1))
    energy = snapshot.raw["energy"][season][period]
    total = sum(energy.values()) + sum(snapshot.raw["adders"].values())
    assert total == pytest.approx(published, abs=5e-6)
    assert snapshot.raw["totals"][season][period] == published


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, TouPeriod.OFF_PEAK),
        (14, TouPeriod.OFF_PEAK),
        (15, TouPeriod.PART_PEAK),
        (16, TouPeriod.PEAK),
        (20, TouPeriod.PEAK),
        (21, TouPeriod.PART_PEAK),
        (23, TouPeriod.PART_PEAK),
    ],
)
def test_tou_period_boundaries(tariff: EelecTariff, hour: int, expected: TouPeriod) -> None:
    assert tariff.period(pt(2026, 7, 15, hour)) is expected


def test_periods_are_identical_every_day_of_the_week(tariff: EelecTariff) -> None:
    """E-ELEC makes no weekday/weekend/holiday distinction at all."""
    # 2026-07-06 is a Monday; the following six days cover the whole week, and
    # 2026-07-03 is an observed holiday.
    days = [pt(2026, 7, 6 + offset, 17) for offset in range(7)] + [pt(2026, 7, 3, 17)]
    assert {tariff.period(day) for day in days} == {TouPeriod.PEAK}


@pytest.mark.parametrize(
    ("year", "month", "day", "expected"),
    [
        (2027, 5, 31, Season.WINTER),
        (2026, 6, 1, Season.SUMMER),
        (2026, 9, 30, Season.SUMMER),
        (2026, 10, 1, Season.WINTER),
        (2027, 1, 15, Season.WINTER),
    ],
)
def test_season_boundaries(
    tariff: EelecTariff, year: int, month: int, day: int, expected: Season
) -> None:
    assert tariff.season(pt(year, month, day, 12)) is expected


def test_dates_before_the_earliest_vendored_sheet_refuse_to_price(
    tariff: EelecTariff,
) -> None:
    """Better to raise than to back-date June's rates onto March."""
    with pytest.raises(Exception, match="no snapshot effective"):
        tariff.price_at(pt(2026, 3, 1, 12))


def test_summer_peak_price(tariff: EelecTariff) -> None:
    assert tariff.price_at(pt(2026, 7, 15, 17)).total == pytest.approx(0.55214)


def test_winter_peak_price(tariff: EelecTariff) -> None:
    """November 4-9pm is winter peak.

    OpenEI's URDB record misfiles these hours as summer off-peak, so this
    doubles as a regression guard against re-importing that bug.
    """
    price = tariff.price_at(pt(2026, 11, 15, 17))
    assert price.season is Season.WINTER
    assert price.period is TouPeriod.PEAK
    assert price.total == pytest.approx(0.32063)
    assert price.total != pytest.approx(0.33358)  # the summer off-peak value


def test_effective_dated_snapshot_selection() -> None:
    """A date before the June sheet must not silently use June's rates."""
    assert load_snapshot("PGE", "E-ELEC", date(2026, 7, 1)).effective == date(2026, 6, 1)
    with pytest.raises(Exception, match="no snapshot effective"):
        load_snapshot("PGE", "E-ELEC", date(2020, 1, 1))


def test_base_services_charge_is_daily_and_excluded_from_energy_price(
    tariff: EelecTariff,
) -> None:
    moment = pt(2026, 7, 15, 17)
    assert tariff.daily_fixed_charge(moment) == pytest.approx(0.79343)
    # Must not leak into the marginal per-kWh price.
    assert "base_services_charge" not in tariff.price_at(moment).components


def test_base_services_charge_tiers() -> None:
    moment = pt(2026, 7, 15, 12)
    charges = [
        EelecTariff(
            Config(base_services_charge_tier=tier, discount=discount, acc_plus_segment=segment)
        ).daily_fixed_charge(moment)
        for tier, discount, segment in (
            (1, "care", "residential_low_income"),
            (2, "fera", "residential_low_income"),
            (3, "none", "residential"),
        )
    ]
    assert charges == pytest.approx([0.19713, 0.39688, 0.79343])


def test_care_discount_drops_wildfire_fund_charge() -> None:
    """CARE sales are not levied the Wildfire Fund Charge at all."""
    moment = pt(2026, 7, 15, 17)
    care = EelecTariff(Config(discount="care", acc_plus_segment="residential_low_income")).price_at(
        moment
    )
    assert "wildfire_fund_charge" not in care.components
    assert care.total == pytest.approx((0.55214 - 0.00591) * 0.65, abs=1e-6)


class TestCca:
    def _config(self, **cca_kwargs: object) -> Config:
        return Config(supplier=Supplier.CCA, cca=CcaConfig(**cca_kwargs))  # type: ignore[arg-type]

    def test_drops_bundled_generation_and_pcia(self) -> None:
        price = EelecTariff(self._config(name="MCE")).price_at(pt(2026, 7, 15, 17))
        assert "generation" not in price.components
        assert "bundled_pcia" not in price.components

    def test_incomplete_without_a_generation_rate_card(self) -> None:
        """Delivery-only must be flagged, not passed off as a full price."""
        price = EelecTariff(self._config(name="MCE")).price_at(pt(2026, 7, 15, 17))
        assert price.complete is False

    def test_complete_with_generation_and_franchise_fee(self) -> None:
        config = self._config(
            name="MCE",
            pcia_vintage=2024,
            franchise_fee_surcharge=0.0009,
            generation_rates={"summer": {"peak": 0.21}},
        )
        price = EelecTariff(config).price_at(pt(2026, 7, 15, 17))
        assert price.complete is True
        assert price.components["cca_generation"] == pytest.approx(0.21)
        assert price.components["pcia"] == pytest.approx(0.05066)
        assert price.components["franchise_fee_surcharge"] == pytest.approx(0.0009)

    def test_unknown_pcia_vintage_raises_rather_than_interpolating(self) -> None:
        config = self._config(name="MCE", pcia_vintage=2015)
        with pytest.raises(ConfigError, match="no PCIA rate vendored"):
            EelecTariff(config).price_at(pt(2026, 7, 15, 17))

    def test_2011_vintage_is_bill_derived_and_within_its_bracket(self) -> None:
        """The 2011 rate is inferred from bills, not read off the tariff sheet.

        PG&E bills a rounded dollar total, so each statement only brackets the
        rate. Two independent periods from the same account, both naming the 2011
        vintage, intersect to a 0.00025 wide window. This pins the vendored value
        inside it, so replacing it with the published figure later is a visible
        change rather than a silent one.
        """
        config = self._config(name="MCE", pcia_vintage=2011)
        rate = EelecTariff(config).price_at(pt(2026, 7, 15, 17)).components["pcia"]

        july = (0.815 / 23.589, 0.825 / 23.589)
        august = (1.385 / 39.906, 1.395 / 39.906)
        low, high = max(july[0], august[0]), min(july[1], august[1])
        assert low < rate < high
        assert rate == pytest.approx((low + high) / 2, abs=5e-6)
