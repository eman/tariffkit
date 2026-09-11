"""Green Button CSV parsing.

PG&E's "Download my data" export is the file these are written against: the
account-metadata preamble before the real header, the split DATE/START TIME
pair, and the unit-suffixed column names it ships. Everything is fed through
StringIO, so no fixture file is needed.
"""

from __future__ import annotations

import io
import stat
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Any

import pytest

from tariffkit.billing import BillingPeriod
from tariffkit.errors import DataError
from tariffkit.sources import (
    GreenButtonLayout,
    PgeSettings,
    cached_bill_periods,
    cached_exports,
    cached_green_button,
    read_green_button,
)
from tariffkit.sources.pge import (
    PortalError,
    _covering,
    _interval_to_period,
    _write_periods,
    available_reads,
    cached_available_reads,
)
from tariffkit.timeutil import export_hour


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


class TestExportCache:
    """The portal generates an export on demand; the same range twice is waste."""

    @staticmethod
    def _settings() -> PgeSettings:
        return PgeSettings(username="person@example.invalid", password="secret")

    @staticmethod
    def _record(
        monkeypatch: pytest.MonkeyPatch, *, available: BillingPeriod | None = None
    ) -> list[tuple[date, date]]:
        """Record what the portal was asked to export, without a portal."""
        asked: list[tuple[date, date]] = []

        class _Session:
            def __init__(self, settings: PgeSettings) -> None: ...

            def __enter__(self) -> _Session:
                return self

            def __exit__(self, *exc: object) -> None: ...

            def login(self) -> None: ...

        def fake(session: object, start: date, end: date) -> str:
            asked.append((start, end))
            # Covering the range asked for, because the file is now named for
            # what it holds -- an export that answers with less is its own
            # test below.
            rows = "".join(
                f"{(start + timedelta(days=n)).isoformat()}T02:00:00-07:00,1.5,0\n"
                for n in range((end - start).days + 1)
            )
            return "start,imported,exported\n" + rows

        monkeypatch.setattr("tariffkit.sources.pge.PgeSession", _Session)
        monkeypatch.setattr("tariffkit.sources.pge.available_reads", lambda session: available)
        monkeypatch.setattr("tariffkit.sources.pge._export_text", fake)
        return asked

    def test_a_second_request_for_the_same_range_reads_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._record(monkeypatch)

        first = cached_green_button(
            self._settings(), date(2026, 7, 1), date(2026, 7, 31), directory=tmp_path
        )
        second = cached_green_button(
            self._settings(), date(2026, 7, 1), date(2026, 7, 31), directory=tmp_path
        )

        assert len(asked) == 1
        assert first.downloaded and not second.downloaded
        assert second.path == first.path
        assert read_green_button(second.path)[0].imported == 1.5

    def test_a_wider_file_serves_a_narrower_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Readings outside a billing period are ignored, so one year serves every cycle."""
        asked = self._record(monkeypatch)

        cached_green_button(
            self._settings(), date(2026, 1, 1), date(2026, 12, 31), directory=tmp_path
        )
        cycle = cached_green_button(
            self._settings(), date(2026, 7, 29), date(2026, 8, 27), directory=tmp_path
        )

        assert len(asked) == 1
        assert not cycle.downloaded
        assert cycle.covers == "2026-01-01..2026-12-31"

    def test_a_range_reaching_past_the_cache_downloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._record(monkeypatch)

        cached_green_button(
            self._settings(), date(2026, 7, 1), date(2026, 7, 31), directory=tmp_path
        )
        cached_green_button(
            self._settings(), date(2026, 7, 1), date(2026, 8, 1), directory=tmp_path
        )

        assert asked == [
            (date(2026, 7, 1), date(2026, 7, 31)),
            (date(2026, 7, 1), date(2026, 8, 1)),
        ]

    def test_refresh_downloads_over_a_cached_range(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._record(monkeypatch)
        window = (date(2026, 7, 1), date(2026, 7, 31))

        cached_green_button(self._settings(), *window, directory=tmp_path)
        again = cached_green_button(self._settings(), *window, directory=tmp_path, refresh=True)

        assert len(asked) == 2
        assert again.downloaded

    def test_the_export_is_private_to_its_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It carries a name, a service address, and every quarter hour of use."""
        self._record(monkeypatch)

        export = cached_green_button(
            self._settings(), date(2026, 7, 1), date(2026, 7, 31), directory=tmp_path
        )

        assert stat.S_IMODE(export.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(export.path.parent.stat().st_mode) == 0o700

    def test_the_narrowest_covering_file_is_the_one_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(monkeypatch)
        cached_green_button(
            self._settings(), date(2026, 1, 1), date(2026, 12, 31), directory=tmp_path
        )
        cached_green_button(
            self._settings(),
            date(2026, 7, 1),
            date(2026, 8, 31),
            directory=tmp_path,
            refresh=True,
        )

        chosen = cached_green_button(
            self._settings(), date(2026, 7, 29), date(2026, 8, 27), directory=tmp_path
        )

        assert chosen.covers == "2026-07-01..2026-08-31"

    def test_a_new_file_drops_the_ranges_it_wholly_contains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Billing an open cycle each day would otherwise leave one file a day."""
        self._record(monkeypatch)
        for last in (date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1)):
            cached_green_button(self._settings(), date(2026, 8, 28), last, directory=tmp_path)

        assert [export.covers for export in cached_exports(tmp_path)] == ["2026-08-28..2026-09-01"]

    def test_a_range_holding_a_day_of_its_own_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overlap is not containment: that file has a day the new one does not."""
        self._record(monkeypatch)
        cached_green_button(
            self._settings(), date(2026, 7, 1), date(2026, 8, 29), directory=tmp_path
        )
        cached_green_button(
            self._settings(), date(2026, 8, 28), date(2026, 9, 1), directory=tmp_path
        )

        assert [export.covers for export in cached_exports(tmp_path)] == [
            "2026-07-01..2026-08-29",
            "2026-08-28..2026-09-01",
        ]

    def test_an_end_past_the_last_published_read_is_pulled_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reads land a day behind, so "through today" would stop short anyway."""
        asked = self._record(
            monkeypatch, available=BillingPeriod(date(2024, 1, 1), date(2026, 9, 8))
        )

        export = cached_green_button(
            self._settings(), date(2026, 8, 28), date(2026, 9, 9), directory=tmp_path
        )

        assert asked == [(date(2026, 8, 28), date(2026, 9, 8))]
        assert export.end == date(2026, 9, 8)
        # Named for what it holds, so tomorrow's lookup is not misled by it.
        assert export.path.name == "2026-08-28_2026-09-08.csv"

    def test_yesterdays_file_answers_todays_open_cycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clamp is what makes the cache work at all for an open cycle."""
        asked = self._record(
            monkeypatch, available=BillingPeriod(date(2024, 1, 1), date(2026, 9, 8))
        )
        cached_green_button(
            self._settings(), date(2026, 8, 28), date(2026, 9, 8), directory=tmp_path
        )

        # Tomorrow morning, before the next read is published.
        again = cached_green_button(
            self._settings(), date(2026, 8, 28), date(2026, 9, 9), directory=tmp_path
        )

        assert len(asked) == 1
        assert not again.downloaded
        assert again.end == date(2026, 9, 8)

    def test_an_end_the_utility_can_cover_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._record(
            monkeypatch, available=BillingPeriod(date(2024, 1, 1), date(2026, 9, 8))
        )

        cached_green_button(
            self._settings(), date(2026, 7, 29), date(2026, 8, 27), directory=tmp_path
        )

        assert asked == [(date(2026, 7, 29), date(2026, 8, 27))]

    def test_a_platform_that_will_not_say_leaves_the_range_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised answer is a reason to ask for what was wanted."""
        asked = self._record(monkeypatch, available=None)

        cached_green_button(
            self._settings(), date(2026, 8, 28), date(2026, 9, 9), directory=tmp_path
        )

        assert asked == [(date(2026, 8, 28), date(2026, 9, 9))]

    def test_an_export_that_answers_with_less_is_named_for_what_it_holds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_logs: Any
    ) -> None:
        """The portal does not always honour the range it is given.

        A real 32-day request came back with its last two days. Naming the file
        for the *request* cached two days under a thirty-two-day name, which
        then answered every later lookup inside that span with almost nothing,
        and let `_drop_superseded` delete a correct narrower file for
        overlapping a range this one only claimed.
        """

        class _Session:
            def __init__(self, settings: object) -> None: ...

            def __enter__(self) -> _Session:
                return self

            def __exit__(self, *exc: object) -> None: ...

            def login(self) -> None: ...

        monkeypatch.setattr("tariffkit.sources.pge.PgeSession", _Session)
        monkeypatch.setattr("tariffkit.sources.pge.available_reads", lambda session: None)
        monkeypatch.setattr(
            "tariffkit.sources.pge._export_text",
            lambda session, start, end: (
                "start,imported,exported\n2026-05-30T02:00:00-07:00,1.5,0\n"
                "2026-05-31T02:00:00-07:00,1.5,0\n"
            ),
        )

        logs = captured_logs("tariffkit.sources.pge")
        export = cached_green_button(
            self._settings(), date(2026, 4, 30), date(2026, 5, 31), directory=tmp_path
        )

        assert export.covers == "2026-05-30..2026-05-31"
        assert export.path.name == "2026-05-30_2026-05-31.csv"
        assert any(
            "the portal exported 2026-05-30..2026-05-31" in r.getMessage() for r in logs.records
        )
        # And it does not answer a later request for days it never held.
        assert _covering(tmp_path, date(2026, 5, 1), date(2026, 5, 15)) is None

    def test_an_export_with_no_readings_at_all_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Session:
            def __init__(self, settings: object) -> None: ...

            def __enter__(self) -> _Session:
                return self

            def __exit__(self, *exc: object) -> None: ...

            def login(self) -> None: ...

        monkeypatch.setattr("tariffkit.sources.pge.PgeSession", _Session)
        monkeypatch.setattr("tariffkit.sources.pge.available_reads", lambda session: None)
        monkeypatch.setattr(
            "tariffkit.sources.pge._export_text",
            lambda session, start, end: "start,imported,exported\n",
        )

        with pytest.raises(PortalError, match="holds no readings"):
            cached_green_button(
                self._settings(), date(2026, 5, 1), date(2026, 5, 31), directory=tmp_path
            )

    def test_a_file_that_is_not_a_range_is_ignored(self, tmp_path: Path) -> None:
        """Anything else in the directory is not an export this wrote."""
        (tmp_path / "notes.csv").write_text("start,imported,exported\n", encoding="utf-8")

        assert cached_exports(tmp_path) == []


class TestBillPeriods:
    """The utility's own cycle boundaries, which beat any guess at a read day."""

    @staticmethod
    def _settings() -> PgeSettings:
        return PgeSettings(username="person@example.invalid", password="secret")

    def test_a_half_open_interval_becomes_an_inclusive_period(self) -> None:
        """The portal's end is exclusive; a billing period's is the day before.

        Both spellings of the same boundary arrive from the same account -- one
        with a Pacific offset, one as UTC -- so both are converted rather than
        sliced.
        """
        assert _interval_to_period("2026-07-29T07:00:00Z/2026-08-28T07:00:00Z") == BillingPeriod(
            date(2026, 7, 29), date(2026, 8, 27)
        )
        assert _interval_to_period(
            "2026-06-03T00:00:00-07:00/2026-06-30T00:00:00-07:00"
        ) == BillingPeriod(date(2026, 6, 3), date(2026, 6, 29))

    def test_an_interval_that_is_not_one_is_refused(self) -> None:
        with pytest.raises(PortalError, match="not an interval"):
            _interval_to_period("whenever")

    def test_a_fresh_cache_answers_without_the_portal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pricing from a local meter must not start needing portal credentials."""
        asked = []
        monkeypatch.setattr(
            "tariffkit.sources.pge.read_bill_periods",
            lambda settings: asked.append(settings) or [],
        )
        store = tmp_path / "bill-periods.json"
        _write_periods(store, [BillingPeriod(date(2026, 7, 29), date(2026, 8, 27))])

        periods = cached_bill_periods(self._settings(), path=store, today=date(2026, 9, 9))

        assert asked == []
        assert periods == [BillingPeriod(date(2026, 7, 29), date(2026, 8, 27))]

    def test_a_cache_that_stopped_covering_the_present_refreshes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bill has been issued that this does not know about."""
        fresh = [BillingPeriod(date(2026, 7, 29), date(2026, 8, 27))]
        monkeypatch.setattr("tariffkit.sources.pge.read_bill_periods", lambda settings: fresh)
        store = tmp_path / "bill-periods.json"
        _write_periods(store, [BillingPeriod(date(2026, 5, 1), date(2026, 5, 31))])

        periods = cached_bill_periods(self._settings(), path=store, today=date(2026, 9, 9))

        assert periods == fresh
        # Written back, so the next command does not ask again.
        assert cached_bill_periods(None, path=store, today=date(2026, 9, 9)) == fresh

    def test_a_refresh_that_cannot_happen_keeps_what_is_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offline is not a reason to fail a bill priced from a local meter."""

        def unreachable(settings: object) -> list[BillingPeriod]:
            raise RuntimeError("getaddrinfo failed")

        monkeypatch.setattr("tariffkit.sources.pge.read_bill_periods", unreachable)
        store = tmp_path / "bill-periods.json"
        stale = [BillingPeriod(date(2026, 5, 1), date(2026, 5, 31))]
        _write_periods(store, stale)

        assert cached_bill_periods(self._settings(), path=store, today=date(2026, 9, 9)) == stale

    def test_without_credentials_it_reports_nothing_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        assert cached_bill_periods(None, path=tmp_path / "absent.json") == []

    def test_an_unreadable_cache_is_a_miss_not_a_failure(self, tmp_path: Path) -> None:
        store = tmp_path / "bill-periods.json"
        store.write_text("{not json", encoding="utf-8")

        assert cached_bill_periods(None, path=store) == []

    def test_the_cache_is_private_to_its_owner(self, tmp_path: Path) -> None:
        """It says when this household is billed, which is nobody else's."""
        store = tmp_path / "nested" / "bill-periods.json"
        _write_periods(store, [BillingPeriod(date(2026, 7, 29), date(2026, 8, 27))])

        assert stat.S_IMODE(store.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700


class TestAvailableReads:
    """How far the utility will actually export, which is not today."""

    @staticmethod
    def _session(payload: object) -> object:
        class _Session:
            def opower(self) -> tuple[str, str, str]:
                return ("pge.opower.com", "token", "urn:opower:v1:account:pge:uuid:x")

            def graphql(self, *args: object, **kwargs: object) -> object:
                return payload

        return _Session()

    @staticmethod
    def _payload(points: list[dict[str, object]]) -> dict[str, object]:
        return {
            "billingAccountByAuthContext": {
                "serviceAgreementsConnection": {"edges": [{"node": {"servicePoints": points}}]}
            }
        }

    def test_the_span_is_the_days_every_channel_covers(self) -> None:
        """Import goes back years and export begins at interconnection."""
        payload = self._payload(
            [
                {
                    # The portal says ELECTRICITY, not ELECTRIC. Matching one
                    # spelling matched nothing and silently disabled the clamp.
                    "serviceType": "ELECTRICITY",
                    "registers": [
                        {
                            "availableReadsTimeInterval": (
                                "2024-04-15T00:00:00-07:00/2026-09-09T00:00:00-07:00"
                            ),
                            "serviceQuantityIdentifier": "DELIVERED",
                            "unitOfMeasure": "KWH",
                        },
                        {
                            "availableReadsTimeInterval": (
                                "2026-05-30T00:00:00-07:00/2026-09-09T00:00:00-07:00"
                            ),
                            "serviceQuantityIdentifier": "RECEIVED",
                            "unitOfMeasure": "KWH",
                        },
                    ],
                }
            ]
        )

        span = available_reads(self._session(payload))

        # Half-open, so the last day with readings is the 8th, not the 9th.
        assert span == BillingPeriod(date(2026, 5, 30), date(2026, 9, 8))

    def test_gas_is_not_electricity(self) -> None:
        """A dual-fuel account must not have its window cut by the gas meter."""
        payload = self._payload(
            [
                {
                    "serviceType": "GAS",
                    "registers": [
                        {
                            "availableReadsTimeInterval": (
                                "2024-01-01T00:00:00-08:00/2026-08-01T00:00:00-07:00"
                            ),
                            "serviceQuantityIdentifier": "DELIVERED",
                            "unitOfMeasure": "THERM",
                        }
                    ],
                },
                {
                    "serviceType": "ELECTRICITY",
                    "registers": [
                        {
                            "availableReadsTimeInterval": (
                                "2024-01-01T00:00:00-08:00/2026-09-09T00:00:00-07:00"
                            ),
                            "serviceQuantityIdentifier": "DELIVERED",
                            "unitOfMeasure": "KWH",
                        }
                    ],
                },
            ]
        )

        assert available_reads(self._session(payload)).end == date(2026, 9, 8)

    def test_an_answer_in_an_unknown_shape_is_not_an_answer(self) -> None:
        assert available_reads(self._session({})) is None
        assert available_reads(self._session(self._payload([]))) is None

    def test_asked_once_a_day_and_no_more(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reads publish daily; a round trip per command would be waste."""
        calls: list[int] = []

        class _Session:
            def __init__(self, settings: object) -> None: ...

            def __enter__(self) -> _Session:
                return self

            def __exit__(self, *exc: object) -> None: ...

            def login(self) -> None: ...

        monkeypatch.setattr("tariffkit.sources.pge.PgeSession", _Session)
        monkeypatch.setattr(
            "tariffkit.sources.pge.available_reads",
            lambda session: calls.append(1) or BillingPeriod(date(2026, 1, 1), date(2026, 9, 8)),
        )
        store = tmp_path / "available-reads.json"
        settings = PgeSettings(username="person@example.invalid", password="secret")

        first = cached_available_reads(settings, path=store, today=date(2026, 9, 9))
        second = cached_available_reads(settings, path=store, today=date(2026, 9, 9))
        tomorrow = cached_available_reads(settings, path=store, today=date(2026, 9, 10))

        span = BillingPeriod(date(2026, 1, 1), date(2026, 9, 8))
        assert first == second == tomorrow == span
        assert len(calls) == 2  # once today, once tomorrow
        assert stat.S_IMODE(store.stat().st_mode) == 0o600

    def test_a_lookup_that_cannot_happen_is_not_an_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offline asks for the range that was wanted, rather than failing."""

        def unreachable(settings: object) -> object:
            raise RuntimeError("getaddrinfo failed")

        monkeypatch.setattr("tariffkit.sources.pge.PgeSession", unreachable)

        assert (
            cached_available_reads(
                PgeSettings(username="person@example.invalid", password="secret"),
                path=tmp_path / "available-reads.json",
            )
            is None
        )

    def test_without_credentials_it_does_not_guess(self, tmp_path: Path) -> None:
        assert cached_available_reads(None, path=tmp_path / "absent.json") is None


def test_a_missing_csv_is_reported_not_raised(tmp_path: Path) -> None:
    """A path that is not there is a thing to report, not an OSError."""
    with pytest.raises(DataError, match="could not read"):
        read_green_button(tmp_path / "absent.csv")


def test_a_parser_failure_is_not_blamed_on_the_portal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "The export holds no readings" is the portal's answer, not ours.

    Swallowing every `DataError` reported an unrecognised header, an
    unparseable timestamp and a non-numeric quantity all as an empty export,
    pointing the reader at PG&E for a regression of ours.
    """
    from tariffkit.sources.pge import _exported_span

    with pytest.raises(DataError, match="no timestamp column"):
        _exported_span("nonsense\n1\n")
    with pytest.raises(DataError, match="could not parse timestamp"):
        _exported_span("start,imported,exported\nnot-a-date,1.0,0\n")
    # Genuinely empty stays the portal's answer to give.
    assert _exported_span("start,imported,exported\n") is None
