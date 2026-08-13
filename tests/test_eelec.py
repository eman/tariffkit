"""E-ELEC import pricing."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from nem_rates import Config, Season, Supplier, TouPeriod
from nem_rates.config import CcaConfig
from nem_rates.errors import ConfigError
from nem_rates.tariff.retail import RetailTariff, load_snapshot
from nem_rates.timeutil import PACIFIC


@pytest.fixture
def tariff() -> RetailTariff:
    return RetailTariff(Config())


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
def test_tou_period_boundaries(tariff: RetailTariff, hour: int, expected: TouPeriod) -> None:
    assert tariff.period(pt(2026, 7, 15, hour)) is expected


def test_periods_are_identical_every_day_of_the_week(tariff: RetailTariff) -> None:
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
    tariff: RetailTariff, year: int, month: int, day: int, expected: Season
) -> None:
    assert tariff.season(pt(year, month, day, 12)) is expected


def test_dates_before_the_earliest_vendored_sheet_refuse_to_price(
    tariff: RetailTariff,
) -> None:
    """Better to raise than to back-date a snapshot onto an earlier month.

    The earliest vintage moves as history is backfilled, so this asks the data
    where its own edge is rather than naming a date that keeps going stale.
    """
    from nem_rates.data import versioned

    earliest = versioned.versions("tariff/pge/eelec")[0].effective
    before = earliest - timedelta(days=1)
    with pytest.raises(Exception, match="no snapshot effective"):
        tariff.price_at(pt(before.year, before.month, before.day, 12))


def test_summer_peak_price(tariff: RetailTariff) -> None:
    assert tariff.price_at(pt(2026, 7, 15, 17)).total == pytest.approx(0.55214)


def test_winter_peak_price(tariff: RetailTariff) -> None:
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
    """A date before the earliest sheet must not silently borrow its rates.

    The snapshot is dated from the sheet carrying the unbundled rate table
    (Advice 7846-E, effective 2026-03-01), not from the later reissue of the
    totals page; see tools/regen_tariff.py::pick_effective.
    """
    assert load_snapshot("PGE", "E-ELEC", date(2026, 7, 1)).effective == date(2026, 3, 1)
    assert load_snapshot("PGE", "E-ELEC", date(2026, 4, 15)).effective == date(2026, 3, 1)
    with pytest.raises(Exception, match="no snapshot effective"):
        load_snapshot("PGE", "E-ELEC", date(2020, 1, 1))


def test_base_services_charge_is_daily_and_excluded_from_energy_price(
    tariff: RetailTariff,
) -> None:
    moment = pt(2026, 7, 15, 17)
    assert tariff.daily_fixed_charge(moment) == pytest.approx(0.79343)
    # Must not leak into the marginal per-kWh price.
    assert "base_services_charge" not in tariff.price_at(moment).components


def test_base_services_charge_tiers() -> None:
    moment = pt(2026, 7, 15, 12)
    charges = [
        RetailTariff(
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
    care = RetailTariff(
        Config(discount="care", acc_plus_segment="residential_low_income")
    ).price_at(moment)
    assert "wildfire_fund_charge" not in care.components
    assert care.total == pytest.approx((0.55214 - 0.00591) * 0.65, abs=1e-6)


class TestCca:
    def _config(self, **cca_kwargs: object) -> Config:
        return Config(supplier=Supplier.CCA, cca=CcaConfig(**cca_kwargs))  # type: ignore[arg-type]

    def test_drops_bundled_generation_and_pcia(self) -> None:
        price = RetailTariff(self._config(name="MCE")).price_at(pt(2026, 7, 15, 17))
        assert "generation" not in price.components
        assert "bundled_pcia" not in price.components

    def test_incomplete_without_a_generation_rate_card(self) -> None:
        """Delivery-only must be flagged, not passed off as a full price."""
        price = RetailTariff(self._config(name="MCE")).price_at(pt(2026, 7, 15, 17))
        assert price.complete is False

    def test_complete_with_generation_and_franchise_fee(self) -> None:
        config = self._config(
            name="MCE",
            pcia_vintage=2024,
            franchise_fee_surcharge=0.0009,
            generation_rates={"summer": {"peak": 0.21}},
        )
        price = RetailTariff(config).price_at(pt(2026, 7, 15, 17))
        assert price.complete is True
        assert price.components["cca_generation"] == pytest.approx(0.21)
        assert price.components["pcia"] == pytest.approx(0.05066)
        assert price.components["franchise_fee_surcharge"] == pytest.approx(0.0009)

    @pytest.mark.parametrize("vintage", [2008, 2027])
    def test_unknown_pcia_vintage_raises_rather_than_interpolating(self, vintage: int) -> None:
        """The sheet publishes 2009 through 2026 and nothing either side.

        2008 falls in the sheet's "Pre-2009" bucket, which is deliberately not
        vendored; 2027 does not exist yet. Both must raise rather than being
        extrapolated off the nearest year.
        """
        config = self._config(name="MCE", pcia_vintage=vintage)
        with pytest.raises(ConfigError, match="no PCIA rate vendored"):
            RetailTariff(config).price_at(pt(2026, 7, 15, 17))

    def test_both_vintaged_tables_cover_the_same_years(self) -> None:
        """Keeps the ConfigError in the franchise fee branch unreachable.

        The two tables come from different schedules that revise independently,
        so it would be easy to extend one and forget the other. Setting
        pcia_vintage is documented as resolving both; this is what makes that
        true rather than true-for-now.
        """
        cca = load_snapshot("PGE", "E-ELEC", date(2026, 6, 1)).raw["cca"]
        assert set(cca["pcia_vintages"]) == set(cca["franchise_fee_vintages"])

    @pytest.mark.parametrize(
        ("vintage", "pcia", "ffs"),
        [
            (2009, 0.02973, 0.00064),
            (2011, 0.03492, 0.00060),
            (2020, 0.03632, 0.00059),
            (2021, 0.05264, 0.00048),
            (2026, -0.01011, 0.00093),
        ],
    )
    def test_vintage_tables_match_the_published_sheets(
        self, vintage: int, pcia: float, ffs: float
    ) -> None:
        """Spot checks across both vintaged tables.

        PCIA from E-ELEC Sheet 5 (Advice 7846-E); franchise fee from Schedule
        E-FFS residential (Advice 7797-E). 2021 is included because that is where
        the PCIA jumps and the franchise fee drops, and 2026 because it is the
        only negative PCIA.
        """
        price = RetailTariff(self._config(name="MCE", pcia_vintage=vintage)).price_at(
            pt(2026, 7, 15, 17)
        )
        assert price.components["pcia"] == pytest.approx(pcia)
        assert price.components["franchise_fee_surcharge"] == pytest.approx(ffs)

    def test_franchise_fee_resolves_from_the_vintage_without_extra_config(self) -> None:
        """It is vintaged off the same year as the PCIA, so one setting covers both."""
        config = self._config(
            name="MCE", pcia_vintage=2011, generation_rates={"summer": {"peak": 0.21}}
        )
        price = RetailTariff(config).price_at(pt(2026, 7, 15, 17))
        assert price.components["franchise_fee_surcharge"] == pytest.approx(0.00060)
        assert price.complete is True

    def test_explicit_franchise_fee_still_wins_over_the_table(self) -> None:
        config = self._config(name="MCE", pcia_vintage=2011, franchise_fee_surcharge=0.00042)
        price = RetailTariff(config).price_at(pt(2026, 7, 15, 17))
        assert price.components["franchise_fee_surcharge"] == pytest.approx(0.00042)

    @pytest.mark.parametrize(
        ("kwh", "pcia_billed", "ffs_billed"), [(23.589, 0.82, 0.01), (39.906, 1.39, 0.02)]
    )
    def test_published_rates_reproduce_billed_dollars(
        self, kwh: float, pcia_billed: float, ffs_billed: float
    ) -> None:
        """Reconciled against two real statements for a 2011-vintage MCE account.

        Checked rate -> dollars rather than dollars -> rate: the billed amounts
        are rounded to the cent, so they cannot pin a five-decimal rate, but an
        exact rate must still reproduce them.
        """
        price = RetailTariff(self._config(name="MCE", pcia_vintage=2011)).price_at(
            pt(2026, 7, 15, 17)
        )
        assert round(price.components["pcia"] * kwh, 2) == pcia_billed
        assert round(price.components["franchise_fee_surcharge"] * kwh, 2) == ffs_billed
