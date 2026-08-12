"""Green Button CSV parsing.

PG&E's "Download my data" export is the file these are written against: the
account-metadata preamble before the real header, the split DATE/START TIME
pair, and the unit-suffixed column names it ships. Everything is fed through
StringIO, so no fixture file is needed.
"""

from __future__ import annotations

import io
from datetime import UTC, timedelta

import pytest

from nem_rates.errors import DataError
from nem_rates.sources import GreenButtonLayout, read_green_button
from nem_rates.timeutil import export_hour


class TestGreenButtonCsv:
    def test_reads_import_export_columns(self) -> None:
        csv_text = (
            "start,imported,exported\n"
            "2026-07-06T02:00:00-07:00,1.5,0\n"
            "2026-07-06T03:00:00-07:00,0,2.5\n"
        )
        readings = read_green_button(io.StringIO(csv_text))
        assert [r.imported for r in readings] == [1.5, 0.0]
        assert [r.exported for r in readings] == [0.0, 2.5]

    def test_reads_a_signed_net_column(self) -> None:
        csv_text = "timestamp,net\n2026-07-06T02:00:00-07:00,1.5\n2026-07-06T03:00:00-07:00,-2.5\n"
        readings = read_green_button(io.StringIO(csv_text), GreenButtonLayout(net="net"))
        assert readings[0].imported == pytest.approx(1.5)
        assert readings[1].exported == pytest.approx(2.5)

    def test_infers_quarter_hour_intervals(self) -> None:
        csv_text = (
            "start,imported\n"
            "2026-07-06T02:00:00-07:00,0.25\n"
            "2026-07-06T02:15:00-07:00,0.25\n"
            "2026-07-06T02:30:00-07:00,0.25\n"
        )
        assert read_green_button(io.StringIO(csv_text))[0].duration == timedelta(minutes=15)

    def test_naive_timestamps_assume_pacific(self) -> None:
        csv_text = "start,imported\n2026-07-06 02:00:00,1.0\n"
        assert read_green_button(io.StringIO(csv_text))[0].start.utcoffset() is not None

    def test_unparseable_number_raises(self) -> None:
        csv_text = "start,imported\n2026-07-06T02:00:00-07:00,abc\n"
        with pytest.raises(DataError, match="as a number"):
            read_green_button(io.StringIO(csv_text))

    def test_missing_energy_column_raises(self) -> None:
        with pytest.raises(DataError, match="no energy column"):
            read_green_button(io.StringIO("start,something\n2026-07-06T02:00:00-07:00,1\n"))

    def test_configured_column_must_exist(self) -> None:
        with pytest.raises(DataError, match="not in header"):
            read_green_button(
                io.StringIO("start,imported\n2026-07-06T02:00:00-07:00,1\n"),
                GreenButtonLayout(imported="nope"),
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
        readings = read_green_button(io.StringIO(csv_text))
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
        fix for such a file is to name the column via ``GreenButtonLayout``, which the
        preamble scan does honour.
        """
        with pytest.raises(DataError, match=r"no timestamp column found in \['Name', 'JANE DOE'\]"):
            read_green_button(io.StringIO("Name,JANE DOE\n\nfoo,bar\n1,2\n"))

    def test_unit_suffixed_column_names_are_matched(self) -> None:
        csv_text = "start,IMPORT (kWh),EXPORT (kWh)\n2026-07-06T02:00:00-07:00,1.5,0.5\n"
        reading = read_green_button(io.StringIO(csv_text))[0]
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
        readings = read_green_button(io.StringIO(csv_text))
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
        assert getattr(read_green_button(io.StringIO(csv_text))[0], attr) == pytest.approx(2.0)

    def test_an_explicit_import_column_still_wins_over_usage(self) -> None:
        """Candidate order decides when a file carries both."""
        csv_text = "start,IMPORT (kWh),USAGE (kWh)\n2026-07-06T02:00:00-07:00,1.5,9.9\n"
        assert read_green_button(io.StringIO(csv_text))[0].imported == pytest.approx(1.5)

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
        assert read_green_button(io.StringIO(csv_text))[0].imported == pytest.approx(1.5)

    def test_a_lone_date_column_holding_a_full_timestamp_still_works(self) -> None:
        """Only pair date with time when both are present; date alone may be ISO."""
        csv_text = "date,imported\n2026-07-06T02:00:00-07:00,1.5\n"
        assert read_green_button(io.StringIO(csv_text))[0].start.hour == 2

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
        readings = read_green_button(io.StringIO(csv_text))
        assert [r.start.fold for r in readings] == [0, 0, 1, 1]
        assert [export_hour(r.start) for r in readings] == [1, 1, 2, 2]
        instants = [r.start.astimezone(UTC) for r in readings]
        assert instants == sorted(instants)
        assert len(set(instants)) == 4

    def test_spring_forward_and_ordinary_days_are_left_alone(self) -> None:
        csv_text = "DATE,START TIME,IMPORT (kWh)\n2027-03-14,01:30,1\n2027-03-14,03:00,1\n"
        assert [r.start.fold for r in read_green_button(io.StringIO(csv_text))] == [0, 0]
        plain = "start,imported\n2026-07-15T01:00:00-07:00,1\n2026-07-15T02:00:00-07:00,1\n"
        assert [r.start.fold for r in read_green_button(io.StringIO(plain))] == [0, 0]

    def test_preamble_skipping_honours_a_configured_column_name(self) -> None:
        """Otherwise the two features do not compose: a custom layout plus a preamble."""
        csv_text = "Name,JANE DOE\n\nwhen,imported\n2026-07-06T02:00:00-07:00,1.5\n"
        reading = read_green_button(io.StringIO(csv_text), GreenButtonLayout(start="when"))[0]
        assert reading.imported == pytest.approx(1.5)

    def test_split_date_time_columns_can_be_configured(self) -> None:
        csv_text = "day,clock,imported\n2026-07-06,02:00,1.5\n"
        reading = read_green_button(
            io.StringIO(csv_text), GreenButtonLayout(date="day", time="clock")
        )[0]
        assert (reading.start.hour, reading.start.day) == (2, 6)
