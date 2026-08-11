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
    CsvLayout,
    IntervalReading,
    check_coverage,
    find_gaps,
    find_overlaps,
    hourly,
    read_csv,
)
from nem_rates.config import CcaConfig
from nem_rates.errors import DataError
from nem_rates.timeutil import PACIFIC, export_hour

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


class TestCsvIngest:
    def test_reads_import_export_columns(self) -> None:
        csv_text = (
            "start,imported,exported\n"
            "2026-07-06T02:00:00-07:00,1.5,0\n"
            "2026-07-06T03:00:00-07:00,0,2.5\n"
        )
        readings = read_csv(io.StringIO(csv_text))
        assert [r.imported for r in readings] == [1.5, 0.0]
        assert [r.exported for r in readings] == [0.0, 2.5]

    def test_reads_a_signed_net_column(self) -> None:
        csv_text = "timestamp,net\n2026-07-06T02:00:00-07:00,1.5\n2026-07-06T03:00:00-07:00,-2.5\n"
        readings = read_csv(io.StringIO(csv_text), CsvLayout(net="net"))
        assert readings[0].imported == pytest.approx(1.5)
        assert readings[1].exported == pytest.approx(2.5)

    def test_infers_quarter_hour_intervals(self) -> None:
        csv_text = (
            "start,imported\n"
            "2026-07-06T02:00:00-07:00,0.25\n"
            "2026-07-06T02:15:00-07:00,0.25\n"
            "2026-07-06T02:30:00-07:00,0.25\n"
        )
        assert read_csv(io.StringIO(csv_text))[0].duration == timedelta(minutes=15)

    def test_naive_timestamps_assume_pacific(self) -> None:
        csv_text = "start,imported\n2026-07-06 02:00:00,1.0\n"
        assert read_csv(io.StringIO(csv_text))[0].start.utcoffset() is not None

    def test_unparseable_number_raises(self) -> None:
        csv_text = "start,imported\n2026-07-06T02:00:00-07:00,abc\n"
        with pytest.raises(DataError, match="as a number"):
            read_csv(io.StringIO(csv_text))

    def test_missing_energy_column_raises(self) -> None:
        with pytest.raises(DataError, match="no energy column"):
            read_csv(io.StringIO("start,something\n2026-07-06T02:00:00-07:00,1\n"))

    def test_configured_column_must_exist(self) -> None:
        with pytest.raises(DataError, match="not in header"):
            read_csv(
                io.StringIO("start,imported\n2026-07-06T02:00:00-07:00,1\n"),
                CsvLayout(imported="nope"),
            )

    def test_reads_pge_interval_export_verbatim(self) -> None:
        """PG&E's own export, which needs three things at once.

        An account preamble before the header, a timestamp split across DATE and
        START TIME, and unit-suffixed column names. Shaped exactly as downloaded
        from My Account, values shortened.
        """
        csv_text = (
            "\n"
            "Name,JANE DOE\n"
            'Address,"1 MAIN ST, SAN RAFAEL CA 94903"\n'
            "Account Number,0000000000\n"
            "Service,0000000000\n"
            "\n"
            "TYPE,DATE,START TIME,END TIME,IMPORT (kWh),EXPORT (kWh),"
            "TOTAL IMPORT COST,TOTAL EXPORT CREDIT (=A+B+C),NOTES\n"
            "Electric usage,2026-07-06,12:00,12:14,0.00,0.31,$0.00,$0.02\n"
            "Electric usage,2026-07-06,12:15,12:29,0.12,0.00,$0.04,$0.00\n"
            "Electric usage,2026-07-06,12:30,12:44,0.00,0.28,$0.00,$0.02\n"
        )
        readings = read_csv(io.StringIO(csv_text))
        assert len(readings) == 3
        assert [r.imported for r in readings] == [0.0, 0.12, 0.0]
        assert [r.exported for r in readings] == [0.31, 0.0, 0.28]
        assert readings[0].duration == timedelta(minutes=15)
        assert readings[0].start.hour == 12
        assert readings[0].start.utcoffset() is not None

    def test_unrecognisable_header_raises_rather_than_guessing(self) -> None:
        """With nothing recognisable anywhere, the file is passed through as-is.

        The error then names the first row, which for a file with a preamble is
        the preamble rather than the real columns. That is the honest outcome:
        picking a header row by guesswork would mis-parse silently instead. The
        fix for such a file is to name the column via ``CsvLayout``, which the
        preamble scan does honour.
        """
        with pytest.raises(DataError, match=r"no timestamp column found in \['Name', 'JANE DOE'\]"):
            read_csv(io.StringIO("Name,JANE DOE\n\nfoo,bar\n1,2\n"))

    def test_unit_suffixed_column_names_are_matched(self) -> None:
        csv_text = "start,IMPORT (kWh),EXPORT (kWh)\n2026-07-06T02:00:00-07:00,1.5,0.5\n"
        reading = read_csv(io.StringIO(csv_text))[0]
        assert reading.imported == pytest.approx(1.5)
        assert reading.exported == pytest.approx(0.5)

    def test_reads_the_export_shape_for_an_account_without_solar(self) -> None:
        """PG&E ships a single USAGE column when there is nothing to export.

        The same download for an exporting account splits into IMPORT and
        EXPORT, so a reader that only knows the latter cannot open a
        pre-solar cycle -- which is exactly the data needed to check a rate
        schedule the account has since left.
        """
        csv_text = (
            "\n"
            "Name,JANE DOE\n"
            "Account Number,0000000000\n"
            "\n"
            "TYPE,DATE,START TIME,END TIME,USAGE (kWh),COST,NOTES\n"
            "Electric usage,2026-01-15,00:00,00:14,0.30,$0.09\n"
            "Electric usage,2026-01-15,00:15,00:29,0.23,$0.09\n"
        )
        readings = read_csv(io.StringIO(csv_text))
        assert [r.imported for r in readings] == [0.30, 0.23]
        assert all(r.exported == 0.0 for r in readings)
        assert readings[0].duration == timedelta(minutes=15)

    @pytest.mark.parametrize(
        ("column", "attr"),
        [
            ("USAGE (kWh)", "imported"),
            ("Consumption (kWh)", "imported"),
            ("Production (kWh)", "exported"),
            ("Net (kWh)", "imported"),
        ],
    )
    def test_a_unit_suffix_does_not_need_its_own_candidate(self, column: str, attr: str) -> None:
        """Matching strips the unit, so each new spelling is not a new entry."""
        csv_text = f"start,{column}\n2026-07-06T02:00:00-07:00,2.0\n"
        assert getattr(read_csv(io.StringIO(csv_text))[0], attr) == pytest.approx(2.0)

    def test_an_explicit_import_column_still_wins_over_usage(self) -> None:
        """Candidate order decides when a file carries both."""
        csv_text = "start,IMPORT (kWh),USAGE (kWh)\n2026-07-06T02:00:00-07:00,1.5,9.9\n"
        assert read_csv(io.StringIO(csv_text))[0].imported == pytest.approx(1.5)

    @pytest.mark.parametrize(
        ("columns", "values"),
        [("IMPORT (kWh),import", "9.9,1.5"), ("import,IMPORT (kWh)", "1.5,9.9")],
        ids=["suffixed first", "exact first"],
    )
    def test_an_exact_name_beats_a_unit_stripped_alias(self, columns: str, values: str) -> None:
        """And does so whichever order the file lists them in.

        Registering aliases in the same pass as exact names let an alias take the
        key first, which made the answer depend on column order: these two
        headers resolved to different columns.
        """
        csv_text = f"start,{columns}\n2026-07-06T02:00:00-07:00,{values}\n"
        assert read_csv(io.StringIO(csv_text))[0].imported == pytest.approx(1.5)

    def test_a_lone_date_column_holding_a_full_timestamp_still_works(self) -> None:
        """Only pair date with time when both are present; date alone may be ISO."""
        csv_text = "date,imported\n2026-07-06T02:00:00-07:00,1.5\n"
        assert read_csv(io.StringIO(csv_text))[0].start.hour == 2

    def test_repeated_hour_on_the_fall_back_day_is_disambiguated(self) -> None:
        """Naive split timestamps are ambiguous on the autumn transition.

        01:00 happens twice and zoneinfo resolves both to fold=0, so without this
        the second pass prices as PG&E's HS1 instead of HS2 and coverage reports
        the file as overlapping itself.
        """
        csv_text = (
            "DATE,START TIME,IMPORT (kWh)\n"
            "2026-11-01,01:00,1\n"
            "2026-11-01,01:30,1\n"
            "2026-11-01,01:00,1\n"
            "2026-11-01,01:30,1\n"
        )
        readings = read_csv(io.StringIO(csv_text))
        assert [r.start.fold for r in readings] == [0, 0, 1, 1]
        assert [export_hour(r.start) for r in readings] == [1, 1, 2, 2]
        instants = [r.start.astimezone(UTC) for r in readings]
        assert instants == sorted(instants)
        assert len(set(instants)) == 4

    def test_spring_forward_and_ordinary_days_are_left_alone(self) -> None:
        csv_text = "DATE,START TIME,IMPORT (kWh)\n2027-03-14,01:30,1\n2027-03-14,03:00,1\n"
        assert [r.start.fold for r in read_csv(io.StringIO(csv_text))] == [0, 0]
        plain = "start,imported\n2026-07-15T01:00:00-07:00,1\n2026-07-15T02:00:00-07:00,1\n"
        assert [r.start.fold for r in read_csv(io.StringIO(plain))] == [0, 0]

    def test_preamble_skipping_honours_a_configured_column_name(self) -> None:
        """Otherwise the two features do not compose: a custom layout plus a preamble."""
        csv_text = "Name,JANE DOE\n\nwhen,imported\n2026-07-06T02:00:00-07:00,1.5\n"
        reading = read_csv(io.StringIO(csv_text), CsvLayout(start="when"))[0]
        assert reading.imported == pytest.approx(1.5)

    def test_split_date_time_columns_can_be_configured(self) -> None:
        csv_text = "day,clock,imported\n2026-07-06,02:00,1.5\n"
        reading = read_csv(io.StringIO(csv_text), CsvLayout(date="day", time="clock"))[0]
        assert (reading.start.hour, reading.start.day) == (2, 6)

    def test_round_trips_into_a_bill(self) -> None:
        csv_text = "start,imported,exported\n" + "".join(
            f"2026-07-06T{h:02d}:00:00-07:00,1.0,0\n" for h in range(24)
        )
        readings = read_csv(io.StringIO(csv_text))
        bill = engine().compute(readings, BillingPeriod(date(2026, 7, 6), date(2026, 7, 6)))
        assert bill.imported_kwh == pytest.approx(24.0)
        assert bill.energy_charges > 0
