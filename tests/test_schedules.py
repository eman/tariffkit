"""Every vendored retail schedule, and the baseline allowance E-TOU-C adds.

Schedule-specific reconciliation against real statements lives in test_eelec.py
and test_mce.py. What is here applies to all of them, so a schedule added later
is covered the moment its data lands.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tariffkit import Config, RateEngine, Season, TouPeriod
from tariffkit.billing.engine import BillEngine
from tariffkit.billing.models import Bill, BillingPeriod, IntervalReading
from tariffkit.errors import ConfigError
from tariffkit.tariff.retail import RetailTariff, load_snapshot
from tariffkit.timeutil import PACIFIC

SCHEDULES = ("E-ELEC", "E-TOU-C", "EV2-A")
EFFECTIVE = date(2026, 6, 1)


def cells() -> list[tuple[str, str, str]]:
    """(tariff, season, period) for every rate the sheets publish."""
    found = []
    for tariff in SCHEDULES:
        totals = load_snapshot("PGE", tariff, EFFECTIVE).raw["totals"]
        for season, periods in totals.items():
            found.extend((tariff, season, period) for period in periods)
    return found


@pytest.mark.parametrize(("tariff", "season", "period"), cells())
def test_components_sum_to_published_total(tariff: str, season: str, period: str) -> None:
    """The unbundled breakdown must reconstruct the published rate exactly.

    This is the check that catches a mistyped digit while transcribing a sheet.
    On E-TOU-C the total is the over-baseline one, because [adders] carries the
    over-baseline Conservation Incentive Adjustment.
    """
    raw = load_snapshot("PGE", tariff, EFFECTIVE).raw
    total = sum(raw["energy"][season][period].values()) + sum(raw["adders"].values())
    assert total == pytest.approx(raw["totals"][season][period], abs=5e-6)


@pytest.mark.parametrize("tariff", SCHEDULES)
def test_every_published_cell_is_reachable_by_pricing(tariff: str) -> None:
    """Guards against a rate table nothing can select, e.g. a stray period."""
    engine = RateEngine(Config(tariff=tariff))
    totals = load_snapshot("PGE", tariff, EFFECTIVE).raw["totals"]
    priced = {
        (str(p.import_price.season), str(p.import_price.period))
        for month in (7, 12)
        for hour in range(24)
        if (p := engine.price_at(datetime(2026, month, 15, hour, tzinfo=PACIFIC)))
    }
    published = {(s, p) for s, periods in totals.items() for p in periods}
    assert priced == published


class TestPeriods:
    def test_etouc_has_no_part_peak_at_all(self) -> None:
        """Peak or off-peak, nothing between -- unlike E-ELEC and EV2-A."""
        tariff = RetailTariff(Config(tariff="E-TOU-C"))
        periods = {
            tariff.period(datetime(2026, 12, 15, hour, tzinfo=PACIFIC)) for hour in range(24)
        }
        assert periods == {TouPeriod.PEAK, TouPeriod.OFF_PEAK}

    @pytest.mark.parametrize(
        ("hour", "expected"),
        [
            (15, TouPeriod.OFF_PEAK),
            (16, TouPeriod.PEAK),
            (20, TouPeriod.PEAK),
            (21, TouPeriod.OFF_PEAK),
        ],
    )
    def test_etouc_boundaries(self, hour: int, expected: TouPeriod) -> None:
        tariff = RetailTariff(Config(tariff="E-TOU-C"))
        assert tariff.period(datetime(2026, 12, 15, hour, tzinfo=PACIFIC)) is expected

    @pytest.mark.parametrize(
        ("hour", "expected"),
        [
            (14, TouPeriod.OFF_PEAK),
            (15, TouPeriod.PART_PEAK),
            (16, TouPeriod.PEAK),
            (21, TouPeriod.PART_PEAK),
            (23, TouPeriod.PART_PEAK),
        ],
    )
    def test_ev2a_keeps_the_part_peak_shoulders(self, hour: int, expected: TouPeriod) -> None:
        tariff = RetailTariff(Config(tariff="EV2-A"))
        assert tariff.period(datetime(2026, 12, 15, hour, tzinfo=PACIFIC)) is expected

    @pytest.mark.parametrize("tariff", SCHEDULES)
    def test_periods_do_not_shift_by_day_of_week(self, tariff: str) -> None:
        """None of these schedules distinguishes weekends or holidays."""
        engine = RetailTariff(Config(tariff=tariff))
        # 2026-07-06 is a Monday; +6 covers the week. 2026-07-03 is observed.
        days = [datetime(2026, 7, 6 + n, 17, tzinfo=PACIFIC) for n in range(7)]
        days.append(datetime(2026, 7, 3, 17, tzinfo=PACIFIC))
        assert {engine.period(day) for day in days} == {TouPeriod.PEAK}


class TestSharedRiders:
    """The PCIA and franchise fee tables are PG&E-wide, reprinted per sheet."""

    @pytest.mark.parametrize("table", ["pcia_vintages", "franchise_fee_vintages"])
    def test_all_schedules_agree(self, table: str) -> None:
        reference = load_snapshot("PGE", "E-ELEC", EFFECTIVE).raw["cca"][table]
        for tariff in SCHEDULES:
            assert load_snapshot("PGE", tariff, EFFECTIVE).raw["cca"][table] == reference, tariff

    @pytest.mark.parametrize("tariff", SCHEDULES)
    def test_base_services_charge_is_the_same_ab205_charge(self, tariff: str) -> None:
        charge = RetailTariff(Config(tariff=tariff)).daily_fixed_charge(
            datetime(2026, 12, 15, 12, tzinfo=PACIFIC)
        )
        assert charge == pytest.approx(0.79343)


class TestDiscounts:
    @pytest.mark.parametrize("tariff", ["E-TOU-C", "EV2-A"])
    def test_unpublished_discount_raises_rather_than_borrowing(self, tariff: str) -> None:
        """The CARE percentage is schedule-specific and these sheets omit it."""
        config = Config(tariff=tariff, discount="care", acc_plus_segment="residential_low_income")
        with pytest.raises(ConfigError, match="does not vendor a CARE discount"):
            RetailTariff(config).price_at(datetime(2026, 12, 15, 17, tzinfo=PACIFIC))

    def test_eelec_still_applies_its_published_discount(self) -> None:
        config = Config(discount="care", acc_plus_segment="residential_low_income")
        price = RetailTariff(config).price_at(datetime(2026, 7, 15, 17, tzinfo=PACIFIC))
        assert price.total == pytest.approx((0.55214 - 0.00591) * 0.65, abs=1e-6)


class TestBaseline:
    def midday(self, month: int, day: int = 15) -> datetime:
        return datetime(2026, month, day, 12, tzinfo=PACIFIC)

    @pytest.mark.parametrize("tariff", ["E-ELEC", "EV2-A"])
    def test_schedules_without_a_baseline_report_no_credit(self, tariff: str) -> None:
        price = RateEngine(Config(tariff=tariff)).price_at(self.midday(12)).import_price
        assert price.baseline_credit == 0.0
        assert RetailTariff(Config(tariff=tariff)).baseline_allowance(self.midday(12)) == 0.0

    def test_credit_is_the_spread_between_the_two_cia_rates(self) -> None:
        """The bill prints one credit; the sheet implements it as two rates."""
        raw = load_snapshot("PGE", "E-TOU-C", EFFECTIVE).raw["baseline"]
        assert raw["over_rate"] - raw["within_rate"] == pytest.approx(raw["credit"])
        price = RateEngine(Config(tariff="E-TOU-C")).price_at(self.midday(12)).import_price
        assert price.baseline_credit == pytest.approx(0.08140)

    def test_marginal_price_is_the_over_baseline_one(self) -> None:
        """Right for dispatch: an allowance is normally spent early in a cycle."""
        price = RateEngine(Config(tariff="E-TOU-C")).price_at(self.midday(12)).import_price
        assert price.total == pytest.approx(0.36757)  # winter off-peak, over baseline
        assert price.total - price.baseline_credit == pytest.approx(0.28617)

    @pytest.mark.parametrize(
        ("territory", "code", "month", "expected"),
        [
            ("X", "basic", 7, 9.8),
            ("X", "basic", 12, 9.7),
            ("X", "all_electric", 12, 14.6),
            ("P", "all_electric", 7, 15.2),
            ("P", "all_electric", 12, 26.0),
            ("Z", "basic", 7, 5.9),
        ],
    )
    def test_allowance_varies_by_territory_season_and_code(
        self, territory: str, code: str, month: int, expected: float
    ) -> None:
        config = Config(tariff="E-TOU-C", baseline_territory=territory, baseline_code=code)  # type: ignore[arg-type]
        assert RetailTariff(config).baseline_allowance(self.midday(month)) == pytest.approx(
            expected
        )

    def test_territory_is_case_insensitive(self) -> None:
        config = Config(tariff="E-TOU-C", baseline_territory="x")
        assert RetailTariff(config).baseline_allowance(self.midday(12)) == pytest.approx(9.7)

    def test_unknown_territory_raises_rather_than_defaulting(self) -> None:
        config = Config(tariff="E-TOU-C", baseline_territory="ZZ")
        with pytest.raises(ConfigError, match="no baseline quantity for territory"):
            RetailTariff(config).baseline_allowance(self.midday(12))

    def test_no_territory_means_no_allowance(self) -> None:
        """Quantities vary several-fold, so guessing one is worse than none."""
        assert RetailTariff(Config(tariff="E-TOU-C")).baseline_allowance(self.midday(12)) == 0.0


class TestBaselineOverACycle:
    def bill(self, readings: list[IntervalReading], period: BillingPeriod, **kw: object) -> Bill:
        config = Config(tariff="E-TOU-C", baseline_territory="X", baseline_code="basic", **kw)  # type: ignore[arg-type]
        return BillEngine(RateEngine(config)).compute(readings, period, check=False)

    def daily(self, start: date, days: int, kwh: float) -> list[IntervalReading]:
        return [
            IntervalReading(
                datetime(start.year, start.month, start.day, 10, tzinfo=PACIFIC)
                + timedelta(days=n),
                imported=kwh,
            )
            for n in range(days)
        ]

    def test_credit_covers_the_allowance_when_usage_exceeds_it(self) -> None:
        period = BillingPeriod(date(2026, 12, 1), date(2026, 12, 30))
        bill = self.bill(self.daily(date(2026, 12, 1), 30, 20.0), period)
        # Territory X winter is 9.7 kWh/day; 30 days at 0.08140.
        assert bill.import_components["baseline_credit"] == pytest.approx(-9.7 * 30 * 0.08140)

    def test_credit_is_capped_by_actual_usage(self) -> None:
        """A light month cannot earn credit on kWh it never imported."""
        period = BillingPeriod(date(2026, 12, 1), date(2026, 12, 30))
        bill = self.bill(self.daily(date(2026, 12, 1), 30, 1.0), period)
        assert bill.import_components["baseline_credit"] == pytest.approx(-30 * 1.0 * 0.08140)

    def test_allowance_accumulates_per_day_across_the_season_boundary(self) -> None:
        """Summer and winter allowances differ, so a day count cannot be used.

        Territory P all-electric is 15.2 summer and 26.0 winter, which makes a
        cycle straddling October 1 clearly wrong if either rate is applied flat.
        """
        period = BillingPeriod(date(2026, 9, 21), date(2026, 10, 20))
        config = Config(tariff="E-TOU-C", baseline_territory="P", baseline_code="all_electric")
        engine = BillEngine(RateEngine(config))
        bill = engine.compute(self.daily(date(2026, 9, 21), 30, 40.0), period, check=False)
        expected = (10 * 15.2 + 20 * 26.0) * 0.08140  # Sep 21-30 summer, Oct 1-20 winter
        assert bill.import_components["baseline_credit"] == pytest.approx(-expected)
        assert expected != pytest.approx(30 * 15.2 * 0.08140)
        assert expected != pytest.approx(30 * 26.0 * 0.08140)

    def test_no_credit_line_without_a_territory(self) -> None:
        period = BillingPeriod(date(2026, 12, 1), date(2026, 12, 30))
        engine = BillEngine(RateEngine(Config(tariff="E-TOU-C")))
        bill = engine.compute(self.daily(date(2026, 12, 1), 30, 20.0), period, check=False)
        assert "baseline_credit" not in bill.import_components

    def test_other_schedules_get_no_credit_line(self) -> None:
        period = BillingPeriod(date(2026, 12, 1), date(2026, 12, 30))
        engine = BillEngine(RateEngine(Config(tariff="EV2-A", baseline_territory="X")))
        bill = engine.compute(self.daily(date(2026, 12, 1), 30, 20.0), period, check=False)
        assert "baseline_credit" not in bill.import_components


def test_season_boundaries_are_shared() -> None:
    for tariff in SCHEDULES:
        engine = RetailTariff(Config(tariff=tariff))
        assert engine.season(datetime(2026, 6, 1, 12, tzinfo=PACIFIC)) is Season.SUMMER
        assert engine.season(datetime(2026, 9, 30, 12, tzinfo=PACIFIC)) is Season.SUMMER
        assert engine.season(datetime(2026, 10, 1, 12, tzinfo=PACIFIC)) is Season.WINTER
