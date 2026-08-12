"""Bill computation, reconciled against a real July 2026 statement.

The statement is PG&E delivery + MCE generation on SBP EELEC, 27 days:

    imports   off-peak 22.903 kWh, part-peak 0.228 kWh, peak 0.458 kWh
    exports   180.68 kWh   (from the ACC Plus credit line, $1.59 @ $0.00880)
    gross energy charges  $8.90
    Base Services Charge  27 days @ $0.79343 = $21.42
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest

from nem_rates import Config, RateEngine, Supplier
from nem_rates.billing import (
    BillEngine,
    BillingPeriod,
    IntervalReading,
    check_coverage,
    find_gaps,
    find_overlaps,
    hourly,
)
from nem_rates.config import CcaConfig
from nem_rates.sources import read_green_button
from nem_rates.timeutil import PACIFIC

BILLED_KWH = 23.589
PERIOD = BillingPeriod(date(2026, 7, 2), date(2026, 7, 28))  # 27 days


def mce_config() -> Config:
    return Config(
        supplier=Supplier.CCA,
        cca=CcaConfig(
            name="MCE",
            rate_card="mce",
            pcia_rate=0.82 / BILLED_KWH,
            franchise_fee_surcharge=0.01 / BILLED_KWH,
        ),
    )


def engine() -> BillEngine:
    return BillEngine(RateEngine(mce_config()))


def pt(day: int, hour: int) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=PACIFIC)


def statement_readings() -> list[IntervalReading]:
    """Readings whose bucket totals match the statement.

    Imports are placed in one representative hour per TOU period; exports at
    midday, where solar actually produces.
    """
    return [
        IntervalReading(pt(6, 2), imported=22.903),  # off-peak
        IntervalReading(pt(6, 15), imported=0.228),  # part-peak
        IntervalReading(pt(6, 17), imported=0.458),  # peak
        IntervalReading(pt(7, 12), exported=180.68),
    ]


class TestReadings:
    def test_from_net_splits_by_sign(self) -> None:
        assert IntervalReading.from_net(pt(6, 2), 5.0).imported == 5.0
        assert IntervalReading.from_net(pt(6, 2), -5.0).exported == 5.0

    def test_from_gross_nets_consumption_against_production(self) -> None:
        r = IntervalReading.from_gross(pt(6, 12), consumption_kwh=1.0, production_kwh=4.0)
        assert r.exported == pytest.approx(3.0)
        assert r.imported == 0.0

    def test_negative_readings_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            IntervalReading(pt(6, 2), imported=-1.0)

    def test_net_is_signed(self) -> None:
        assert IntervalReading(pt(6, 2), imported=3.0, exported=1.0).net == pytest.approx(2.0)


class TestBillingPeriod:
    def test_days_is_inclusive(self) -> None:
        assert PERIOD.days == 27

    def test_rejects_backwards_period(self) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            BillingPeriod(date(2026, 7, 28), date(2026, 7, 2))

    def test_inferred_from_readings(self) -> None:
        inferred = BillingPeriod.from_readings(statement_readings())
        assert inferred.start == date(2026, 7, 6)
        assert inferred.end == date(2026, 7, 7)


class TestStatementReconciliation:
    @pytest.fixture
    def bill(self) -> object:
        return engine().compute(statement_readings(), PERIOD)

    def test_import_buckets_match_the_statement(self, bill) -> None:  # type: ignore[no-untyped-def]
        by_period = {str(b.period): b for b in bill.buckets}
        assert by_period["off_peak"].imported == pytest.approx(22.903)
        assert by_period["part_peak"].imported == pytest.approx(0.228)
        assert by_period["peak"].imported == pytest.approx(0.458)

    def test_bucket_rates_match_the_marginal_rates(self, bill) -> None:  # type: ignore[no-untyped-def]
        by_period = {str(b.period): b for b in bill.buckets}
        assert by_period["off_peak"].import_rate == pytest.approx(0.37267, abs=1e-5)
        assert by_period["part_peak"].import_rate == pytest.approx(0.42935, abs=1e-5)
        assert by_period["peak"].import_rate == pytest.approx(0.59123, abs=1e-5)

    def test_gross_energy_charges(self, bill) -> None:  # type: ignore[no-untyped-def]
        """The statement's six per-kWh lines sum to $8.90."""
        assert bill.energy_charges == pytest.approx(8.90, abs=0.01)

    def test_base_services_charge(self, bill) -> None:  # type: ignore[no-untyped-def]
        assert bill.fixed_components["base_services_charge"] == pytest.approx(21.42, abs=0.01)

    def test_components_decompose_the_way_the_bill_prints(self, bill) -> None:  # type: ignore[no-untyped-def]
        c = bill.import_components
        # MCE generation across all three periods.
        assert c["cca_generation"] == pytest.approx(2.88, abs=0.01)
        assert c["cca_cost_relief_credit"] == pytest.approx(-0.15, abs=0.01)
        assert c["pcia"] == pytest.approx(0.82, abs=0.01)
        assert c["franchise_fee_surcharge"] == pytest.approx(0.01, abs=0.01)

    def test_acc_plus_credit_matches_the_billed_line(self, bill) -> None:  # type: ignore[no-untyped-def]
        """Statement shows '@ $0.00880  -1.59' against 180.68 kWh exported."""
        assert bill.export_components["acc_plus"] == pytest.approx(-1.59, abs=0.01)

    def test_export_credits_are_negative(self, bill) -> None:  # type: ignore[no-untyped-def]
        assert bill.export_credits < 0
        assert bill.exported_kwh == pytest.approx(180.68)

    def test_totals_sum_consistently(self, bill) -> None:  # type: ignore[no-untyped-def]
        assert bill.total == pytest.approx(
            bill.energy_charges + bill.export_credits + bill.fixed_charges
        )

    def test_pricing_confidence_is_separate_from_coverage(self, bill) -> None:  # type: ignore[no-untyped-def]
        """These readings price cleanly but cover the period sparsely.

        Four representative readings stand in for 27 days, so coverage warnings
        are expected while every rate applied is fully known. Folding the two
        together used to make a bill that reconciles against a real statement
        still describe itself as an estimate.
        """
        assert bill.complete is True
        assert bill.warnings

    def test_serializes(self, bill) -> None:  # type: ignore[no-untyped-def]
        payload = bill.to_dict()
        assert payload["period"]["days"] == 27
        assert payload["imported_kwh"] == pytest.approx(23.589)


class TestPeriodElapsed:
    """days x 24h is not the real span of a cycle containing a DST transition."""

    @pytest.mark.parametrize(
        ("start", "end", "days", "hours"),
        [
            (date(2026, 6, 30), date(2026, 7, 28), 29, 29 * 24),
            (date(2025, 10, 29), date(2025, 11, 30), 33, 33 * 24 + 1),  # fall back
            (date(2026, 3, 3), date(2026, 3, 31), 29, 29 * 24 - 1),  # spring forward
        ],
        ids=["ordinary", "fall back", "spring forward"],
    )
    def test_elapsed_counts_real_hours(self, start: date, end: date, days: int, hours: int) -> None:
        period = BillingPeriod(start, end)
        assert period.days == days
        assert period.elapsed == timedelta(hours=hours)

    def test_days_still_counts_calendar_days(self) -> None:
        """The Base Services Charge is billed per calendar day, not per 24 hours."""
        assert BillingPeriod(date(2025, 10, 29), date(2025, 11, 30)).days == 33


class TestCoverageChecks:
    def _full_day(self) -> list[IntervalReading]:
        return [IntervalReading(pt(6, h), imported=1.0) for h in range(24)]

    def test_contiguous_day_has_no_warnings(self) -> None:
        period = BillingPeriod(date(2026, 7, 6), date(2026, 7, 6))
        assert list(check_coverage(self._full_day(), period)) == []

    def test_gap_is_reported(self) -> None:
        readings = [r for r in self._full_day() if r.start.hour != 5]
        period = BillingPeriod(date(2026, 7, 6), date(2026, 7, 6))
        warnings = list(check_coverage(readings, period))
        assert any("gap" in w for w in warnings)
        assert len(list(find_gaps(readings))) == 1

    def test_overlap_is_reported(self) -> None:
        readings = [
            IntervalReading(pt(6, 0), imported=1.0, duration=timedelta(hours=2)),
            IntervalReading(pt(6, 1), imported=1.0),
        ]
        assert len(list(find_overlaps(readings))) == 1

    def test_a_missing_hour_on_the_fall_back_day_is_reported(self) -> None:
        """The autumn transition hides an hour from wall-clock arithmetic.

        PG&E's own export emits 96 intervals for that 25-hour day: the repeated
        01:00 hour is simply absent. Measured on the clock face, 01:45 plus
        fifteen minutes reads as 02:00 and the missing hour vanishes -- which is
        precisely the silently-short bill coverage checking exists to catch.
        """
        readings = [
            IntervalReading(
                datetime(2025, 11, 2, 1, 45, tzinfo=PACIFIC),
                imported=1.0,
                duration=timedelta(minutes=15),
            ),
            IntervalReading(
                datetime(2025, 11, 2, 2, 0, tzinfo=PACIFIC),
                imported=1.0,
                duration=timedelta(minutes=15),
            ),
        ]
        gaps = list(find_gaps(readings))
        assert len(gaps) == 1
        start, end = gaps[0]
        assert (end.astimezone(UTC) - start.astimezone(UTC)) == timedelta(hours=1)

    def test_the_spring_forward_jump_is_not_a_gap(self) -> None:
        """The labels skip an hour that never existed, so the series is contiguous.

        PG&E writes a nonexistent 02:00 and then resumes at 03:15; both resolve to
        instants fifteen minutes apart.
        """
        readings = [
            IntervalReading(
                datetime(2026, 3, 8, 2, 0, tzinfo=PACIFIC),
                imported=1.0,
                duration=timedelta(minutes=15),
            ),
            IntervalReading(
                datetime(2026, 3, 8, 3, 15, tzinfo=PACIFIC),
                imported=1.0,
                duration=timedelta(minutes=15),
            ),
        ]
        assert list(find_gaps(readings)) == []
        assert list(find_overlaps(readings)) == []

    def test_the_repeated_hour_is_not_an_overlap(self) -> None:
        """Two 01:00 readings an hour apart in real time are contiguous, not overlapping."""
        readings = [
            IntervalReading(
                datetime(2026, 11, 1, 1, 0, fold=0, tzinfo=PACIFIC),
                imported=1.0,
                duration=timedelta(hours=1),
            ),
            IntervalReading(
                datetime(2026, 11, 1, 1, 0, fold=1, tzinfo=PACIFIC),
                imported=1.0,
                duration=timedelta(hours=1),
            ),
        ]
        assert list(find_overlaps(readings)) == []
        assert list(find_gaps(readings)) == []

    def test_missing_coverage_is_reported(self) -> None:
        warnings = list(check_coverage(self._full_day(), PERIOD))
        assert any("missing" in w for w in warnings)

    def test_no_readings_is_reported(self) -> None:
        assert list(check_coverage([], PERIOD)) == [f"no readings in {PERIOD.start}..{PERIOD.end}"]

    def test_both_directions_in_one_interval_is_reported(self) -> None:
        readings = [IntervalReading(pt(6, 0), imported=1.0, exported=1.0)]
        period = BillingPeriod(date(2026, 7, 6), date(2026, 7, 6))
        assert any("both import and export" in w for w in check_coverage(readings, period))

    def test_gappy_data_warns_without_impugning_the_prices(self) -> None:
        """A bill over a lossy series must not look like a light-usage month.

        The gap surfaces as a warning. It does not clear ``complete``, which is
        a claim about the rates rather than the readings.
        """
        readings = [r for r in self._full_day() if r.start.hour != 5]
        bill = engine().compute(readings, BillingPeriod(date(2026, 7, 6), date(2026, 7, 6)))
        assert any("gap" in w for w in bill.warnings)
        assert bill.complete is True

    def test_unverified_prices_clear_complete_even_with_perfect_coverage(self) -> None:
        """The other half of the split, so neither signal can absorb the other.

        A CCA with no generation rate card prices delivery only, which is a real
        pricing gap, over a day of readings with nothing wrong in them.
        """
        bare = Config(supplier=Supplier.CCA, cca=CcaConfig(name="Unknown CCA"))
        period = BillingPeriod(date(2026, 7, 6), date(2026, 7, 6))
        bill = BillEngine(RateEngine(bare)).compute(self._full_day(), period)
        assert bill.complete is False
        assert bill.warnings == ()

    def test_checks_can_be_skipped(self) -> None:
        bill = engine().compute(self._full_day(), PERIOD, check=False)
        assert bill.warnings == ()


class TestSubHourly:
    def test_quarter_hours_aggregate_to_the_same_charge(self) -> None:
        quarters = [
            IntervalReading(
                pt(6, 2) + timedelta(minutes=15 * i), imported=0.25, duration=timedelta(minutes=15)
            )
            for i in range(4)
        ]
        whole = [IntervalReading(pt(6, 2), imported=1.0)]
        period = BillingPeriod(date(2026, 7, 6), date(2026, 7, 6))
        a = engine().compute(quarters, period, check=False).energy_charges
        b = engine().compute(whole, period, check=False).energy_charges
        assert a == pytest.approx(b)

    def test_hourly_preserves_both_sides_separately(self) -> None:
        """Collapsing must not net across the finer intervals."""
        readings = [
            IntervalReading(pt(6, 12), imported=1.0, duration=timedelta(minutes=30)),
            IntervalReading(
                pt(6, 12) + timedelta(minutes=30), exported=3.0, duration=timedelta(minutes=30)
            ),
        ]
        collapsed = hourly(readings)
        assert len(collapsed) == 1
        assert collapsed[0].imported == pytest.approx(1.0)
        assert collapsed[0].exported == pytest.approx(3.0)


class TestGreenButtonRoundTrip:
    """The reader itself is tested in test_sources_greenbutton.py; this is the
    seam between it and the engine."""

    def test_round_trips_into_a_bill(self) -> None:
        csv_text = "start,imported,exported\n" + "".join(
            f"2026-07-06T{h:02d}:00:00-07:00,1.0,0\n" for h in range(24)
        )
        readings = read_green_button(io.StringIO(csv_text))
        bill = engine().compute(readings, BillingPeriod(date(2026, 7, 6), date(2026, 7, 6)))
        assert bill.imported_kwh == pytest.approx(24.0)
        assert bill.energy_charges > 0


class TestPeriodsWithoutExports:
    """A cycle before interconnection has no net-billing arrangement.

    PG&E's own export for the December 2025 cycle carries zero exported kWh in
    all 2,880 intervals -- there was no export channel to meter. Demanding an
    export rate for such a period refuses to price a bill over a question the
    data never asks, and export-rate matrices only start at the customer's own
    NBT vintage year.
    """

    def readings(self) -> list[IntervalReading]:
        start = datetime(2025, 12, 30, tzinfo=PACIFIC)
        return [
            IntervalReading(
                start=start + timedelta(hours=h),
                imported=1.0,
                exported=0.0,
                duration=timedelta(hours=1),
            )
            for h in range(48)
        ]

    def test_a_cycle_with_no_exports_prices_before_the_export_vintage(self) -> None:
        bill = engine().compute(
            self.readings(), BillingPeriod(date(2025, 12, 30), date(2025, 12, 31)), check=False
        )
        assert sum(b.imported for b in bill.buckets) == pytest.approx(48.0)
        assert bill.energy_charges > 0

    def test_no_export_components_are_invented(self) -> None:
        bill = engine().compute(
            self.readings(), BillingPeriod(date(2025, 12, 30), date(2025, 12, 31)), check=False
        )
        assert bill.export_components == {}
        assert all(b.exported == 0 for b in bill.buckets)
