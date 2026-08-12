"""Engine composition, forecasting, DST behaviour, and config validation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from nem_rates import Config, RateEngine, Supplier
from nem_rates.config import CcaConfig
from nem_rates.errors import ConfigError, OutOfRangeError
from nem_rates.timeutil import (
    PACIFIC,
    DayType,
    day_type,
    export_hour,
    holidays,
    is_holiday,
    next_hour,
)


def pt(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=PACIFIC)


@pytest.fixture
def engine() -> RateEngine:
    return RateEngine()


class TestKnownValues:
    """Spot checks measured directly from PG&E's published files."""

    def test_september_evening_peak(self, engine: RateEngine) -> None:
        point = engine.price_at(pt(2026, 9, 15, 19))
        assert point.export_price.components["generation"] == pytest.approx(0.59312)
        assert point.export_price.components["delivery"] == pytest.approx(0.00193)
        assert point.export_price.components["acc_plus"] == pytest.approx(0.00880)
        assert point.export_price.total == pytest.approx(0.60385)

    def test_september_midday_trough(self, engine: RateEngine) -> None:
        point = engine.price_at(pt(2026, 9, 15, 13))
        # 0.06155 base + 0.00880 ACC Plus
        assert point.export_price.total == pytest.approx(0.07035)

    def test_august_weekend_is_the_annual_peak(self, engine: RateEngine) -> None:
        # 2026-08-15 is a Saturday. 1.19289 base + 0.00880 ACC Plus.
        point = engine.price_at(pt(2026, 8, 15, 19))
        assert point.export_price.day_type is DayType.WEEKEND
        assert point.export_price.total == pytest.approx(1.20169)

    def test_exporting_beats_self_consumption_on_a_september_evening(
        self, engine: RateEngine
    ) -> None:
        point = engine.price_at(pt(2026, 9, 15, 19))
        assert point.import_price.total == pytest.approx(0.55214)
        assert point.spread > 0

    def test_export_credit_is_never_negative(self, engine: RateEngine) -> None:
        """Upstream floors each component at zero before summing."""
        curve = engine.forecast(hours=24 * 30, start=pt(2026, 6, 1, 0))
        assert min(p.export_price.total for p in curve) >= 0.0


class TestHolidays:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 7, 3), True),  # Jul 4 is a Saturday, observed Friday
            (date(2026, 7, 4), False),
            (date(2027, 7, 5), True),  # Jul 4 is a Sunday, observed Monday
            (date(2027, 12, 24), True),  # Christmas falls on a Saturday
            (date(2027, 12, 31), True),  # New Year's Day 2028 is a Saturday
            (date(2028, 11, 10), True),  # Veterans Day is a Saturday
            (date(2026, 1, 19), False),  # MLK Day is not a tariff holiday
            (date(2026, 6, 19), False),  # nor is Juneteenth
        ],
    )
    def test_observed_dates(self, day: date, expected: bool) -> None:
        assert is_holiday(day) is expected

    def test_eight_holidays_shifted_by_observed_dates(self) -> None:
        """Eight holidays a year, but not eight per *calendar* year.

        New Year's Day 2028 falls on a Saturday, so it is observed on
        2027-12-31 -- giving 2027 nine and 2028 seven.
        """
        assert len(holidays(2026)) == 8
        assert len(holidays(2027)) == 9
        assert len(holidays(2028)) == 7
        assert sum(len(holidays(y)) for y in (2026, 2027, 2028)) == 24

    def test_holiday_uses_the_weekend_column(self) -> None:
        assert day_type(pt(2026, 7, 3, 12)) is DayType.WEEKEND
        assert day_type(pt(2026, 7, 2, 12)) is DayType.WEEKDAY


class TestDaylightSaving:
    def test_spring_forward_day_has_23_hours(self, engine: RateEngine) -> None:
        # 2027-03-14, the first spring-forward covered by the vendored tariff.
        curve = engine.forecast(hours=24, start=pt(2027, 3, 14, 0))
        hours = [p.start.hour for p in curve]
        assert 2 not in hours  # 02:00 PST->PDT never happens
        assert curve[-1].start.day == 15

    def test_fall_back_day_has_25_hours(self, engine: RateEngine) -> None:
        curve = engine.forecast(hours=25, start=pt(2026, 11, 1, 0))
        assert [p.start.hour for p in curve].count(1) == 2
        assert curve[-1].start.day == 1
        assert curve[-1].start.hour == 23

    @pytest.mark.parametrize(
        ("start", "hours"),
        [(pt(2026, 11, 1, 0), 25), (pt(2027, 3, 14, 0), 23)],
        ids=["fall-back", "spring-forward"],
    )
    def test_every_point_spans_exactly_one_real_hour(
        self, engine: RateEngine, start: datetime, hours: int
    ) -> None:
        """``end`` must be an hour of absolute time, not of wall clock.

        Computed by wall clock, the fall-back day's first 01:00 ran to 02:00 PST --
        two real hours, overlapping the second 01:00 entirely. Consumers that read
        the explicit start/end pairs (Predbat) need them contiguous and disjoint.
        """
        curve = engine.forecast(hours=hours, start=start)
        for point in curve:
            span = point.end.astimezone(UTC) - point.start.astimezone(UTC)
            assert span == timedelta(hours=1), f"{point.start.isoformat()} spans {span}"

        instants = [p.start.astimezone(UTC) for p in curve]
        assert len(set(instants)) == len(instants)
        for earlier, later in zip(curve.points, curve.points[1:], strict=False):
            assert earlier.end.astimezone(UTC) == later.start.astimezone(UTC)

    def test_next_hour_advances_one_real_hour(self) -> None:
        """The MQTT publisher sleeps until this.

        Computed by wall clock, the first 01:00 PDT advanced to 02:00 PST -- two
        hours -- so an hourly publisher skipped the second 01:00 entirely and
        left its retained topics an hour stale.
        """
        first = datetime(2026, 11, 1, 1, tzinfo=PACIFIC, fold=0)
        second = next_hour(first)
        assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)
        assert (second.hour, second.utcoffset()) == (1, timedelta(hours=-8))
        # ...and from the repeated hour it finally moves on to 02:00.
        assert next_hour(second).hour == 2

    @pytest.mark.parametrize("hour", [0, 5, 12, 23])
    def test_next_hour_is_an_hour_away_on_ordinary_days(self, hour: int) -> None:
        moment = pt(2026, 7, 15, hour)
        assert next_hour(moment).astimezone(UTC) - moment.astimezone(UTC) == timedelta(hours=1)

    def test_repeated_hour_is_priced_as_hour_two(self) -> None:
        """PG&E gives the second 01:00 the HS2 label, so it must price as 2am."""
        first = datetime(2026, 11, 1, 1, tzinfo=PACIFIC, fold=0)
        second = datetime(2026, 11, 1, 1, tzinfo=PACIFIC, fold=1)
        assert export_hour(first) == 1
        assert export_hour(second) == 2
        assert first.utcoffset() != second.utcoffset()

    def test_forecast_steps_in_absolute_time(self, engine: RateEngine) -> None:
        curve = engine.forecast(hours=26, start=pt(2026, 11, 1, 0))
        for earlier, later in zip(curve.points, curve.points[1:], strict=False):
            assert later.start.astimezone(UTC) - earlier.start.astimezone(UTC) == timedelta(hours=1)


class TestLockWindow:
    def test_lock_end_is_nine_years_from_pto(self) -> None:
        assert Config().lock_end == date(2035, 6, 2)

    def test_inside_the_window_is_locked(self, engine: RateEngine) -> None:
        assert engine.price_at(pt(2035, 6, 1, 12)).export_price.locked is True

    def test_outside_the_window_is_not(self, engine: RateEngine) -> None:
        assert engine.price_at(pt(2035, 6, 3, 12)).export_price.locked is False

    def test_floating_vintage_has_no_lock(self) -> None:
        config = Config(vintage="NBT00", interconnection_year=None, pto_date=None)
        assert config.lock_end is None
        assert RateEngine(config).price_at(pt(2026, 9, 15, 19)).export_price.locked is False

    def test_acc_plus_is_flat_across_the_whole_lock(self, engine: RateEngine) -> None:
        """The published step-down applies to later applicants, not over time."""
        for year in (2026, 2030, 2035):
            point = engine.price_at(pt(year, 9, 15, 19))
            assert point.export_price.components["acc_plus"] == pytest.approx(0.00880)


class TestVintageResolution:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [(2023, "NBT23"), (2024, "NBT24"), (2025, "NBT25"), (2026, "NBT26"), (2030, "NBT00")],
    )
    def test_from_interconnection_year(self, year: int, expected: str) -> None:
        assert Config(interconnection_year=year, pto_date=None).resolved_vintage == expected

    def test_explicit_vintage_wins(self) -> None:
        assert Config(interconnection_year=2026, vintage="NBT23").resolved_vintage == "NBT23"

    def test_default_config_targets_nbt26(self, engine: RateEngine) -> None:
        assert engine.export_rates.vintage == "NBT26"
        assert engine.export_rates.acc_plus == pytest.approx(0.00880)


class TestCoverage:
    def test_year_outside_the_data_raises(self, engine: RateEngine) -> None:
        with pytest.raises(OutOfRangeError, match="no export rates"):
            engine.price_at(pt(2060, 1, 1, 12))

    def test_exact_through_covers_the_lock(self, engine: RateEngine) -> None:
        assert engine.export_rates.exact_through >= 2035

    def test_prices_beyond_exact_through_are_flagged(self, engine: RateEngine) -> None:
        assert engine.price_at(pt(2026, 9, 15, 19)).export_price.exact is True
        assert engine.price_at(pt(2044, 9, 15, 19)).export_price.exact is False


class TestForecast:
    def test_length_and_contiguity(self, engine: RateEngine) -> None:
        curve = engine.forecast(hours=48, start=pt(2026, 9, 15, 0))
        assert len(curve) == 48
        for earlier, later in zip(curve.points, curve.points[1:], strict=False):
            assert earlier.end == later.start

    def test_best_export_hours_lands_on_the_evening_ramp(self, engine: RateEngine) -> None:
        curve = engine.forecast(hours=24, start=pt(2026, 9, 15, 0))
        assert [p.start.hour for p in curve.best_export_hours(3)] == [18, 19, 20]

    def test_cheapest_import_hours_are_off_peak(self, engine: RateEngine) -> None:
        curve = engine.forecast(hours=24, start=pt(2026, 9, 15, 0))
        assert all(p.start.hour < 15 for p in curve.cheapest_import_hours(3))

    def test_rejects_nonpositive_horizon(self, engine: RateEngine) -> None:
        with pytest.raises(ValueError, match="hours must be"):
            engine.forecast(hours=0)

    def test_serializes(self, engine: RateEngine) -> None:
        payload = engine.forecast(hours=3, start=pt(2026, 9, 15, 0)).to_dict()
        assert payload["hours"] == 3
        assert payload["points"][0]["export"]["vintage"] == "NBT26"


class TestConfigValidation:
    def test_naive_datetime_is_rejected(self, engine: RateEngine) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            engine.price_at(datetime(2026, 9, 15, 19))

    def test_cca_supplier_requires_cca_config(self) -> None:
        with pytest.raises(ConfigError, match="requires a CcaConfig"):
            Config(supplier=Supplier.CCA)

    def test_discount_requires_matching_acc_plus_segment(self) -> None:
        with pytest.raises(ConfigError, match="residential_low_income"):
            Config(discount="care")

    def test_low_income_acc_plus_is_much_larger(self) -> None:
        config = Config(discount="care", acc_plus_segment="residential_low_income")
        assert RateEngine(config).export_rates.acc_plus == pytest.approx(0.03600)

    def test_needs_a_vintage_source(self) -> None:
        with pytest.raises(ConfigError, match="interconnection_year or vintage"):
            Config(interconnection_year=None)

    def test_from_dict_round_trip(self) -> None:
        config = Config.from_dict(
            {
                "supplier": "cca",
                "interconnection_year": 2026,
                "pto_date": "2026-06-03",
                "cca": {"name": "MCE", "pcia_vintage": 2024},
            }
        )
        assert config.supplier is Supplier.CCA
        assert config.pto_date == date(2026, 6, 3)
        assert config.cca is not None
        assert config.cca.name == "MCE"

    def test_from_dict_rejects_unknown_keys(self) -> None:
        with pytest.raises(ConfigError, match="unknown config keys"):
            Config.from_dict({"supplier": "bundled", "nonsense": 1})


class TestCcaExport:
    def test_delivery_only_and_flagged_incomplete(self) -> None:
        config = Config(supplier=Supplier.CCA, cca=CcaConfig(name="MCE"))
        price = RateEngine(config).price_at(pt(2026, 9, 15, 19)).export_price
        assert "generation" not in price.components
        assert price.components["delivery"] == pytest.approx(0.00193)
        assert price.complete is False

    def test_cca_export_rate_is_used_when_supplied(self) -> None:
        config = Config(
            supplier=Supplier.CCA,
            cca=CcaConfig(name="MCE", export_generation_rate=0.5),
        )
        price = RateEngine(config).price_at(pt(2026, 9, 15, 19)).export_price
        assert price.components["cca_generation"] == pytest.approx(0.5)
        assert price.complete is True


def test_describe_reports_provenance(engine: RateEngine) -> None:
    info = engine.describe()
    assert info["export_vintage"] == "NBT26"
    assert info["tariff_effective"] == "2026-03-01"
    assert info["tariff_advice_letter"] == "7846-E"
    assert info["lock_end"] == "2035-06-02"
