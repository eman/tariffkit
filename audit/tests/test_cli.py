"""The command line's contract, which is mostly its exit codes.

The codes are the part other things depend on -- a shell loop, a cron entry, a
human reading a summary -- so they are pinned here rather than left to whatever
the dispatch happens to return.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from audit import __version__
from audit.cli import EXIT_ERROR, EXIT_MISMATCH, EXIT_OK, build_parser, main
from audit.run import BillRef, downloaded, statement_cache, statement_name, statement_names


class TestExitCodes:
    def test_the_three_codes_are_distinct(self) -> None:
        # A disagreement and a broken harness must not be the same answer: they
        # call for opposite responses from whoever reads them.
        assert len({EXIT_OK, EXIT_MISMATCH, EXIT_ERROR}) == 3

    def test_success_is_zero(self) -> None:
        assert EXIT_OK == 0


class TestParser:
    def test_version_reports_both_versions(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The harness and the library it audits version separately, and a report
        # is only reproducible if you know which of each produced it.
        from tariffkit import __version__ as library_version

        with pytest.raises(SystemExit) as caught:
            main(["--version"])
        assert caught.value.code == 0
        printed = capsys.readouterr().out
        assert __version__ in printed
        assert library_version in printed

    def test_no_arguments_prints_help_and_succeeds(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([]) == EXIT_OK
        assert "Reconcile computed bills" in capsys.readouterr().out

    def test_a_run_keeps_its_statements_unless_told_otherwise(self) -> None:
        assert build_parser().parse_args(["run"]).discard_statements is False
        assert build_parser().parse_args(["run", "--discard-statements"]).discard_statements

    def test_an_unknown_flag_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(["--nonsense"])
        assert caught.value.code == 2

    def test_a_check_that_could_not_run_exits_two_not_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # "I could not check" and "your numbers disagree" call for opposite
        # responses. An AuditError escaping to Python gives exit 1, which reads
        # as a billing discrepancy that was never actually found.
        #
        # The config home is redirected because this has to be the same answer
        # on a machine that has an account as on one that does not: pointed at
        # a real ~/.config, the run got past "no account" and failed later for
        # an unrelated reason, and the test passed or failed by whose laptop it
        # was on.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        code = main(["reconcile", str(tmp_path / "nope.pdf")])
        assert code == EXIT_ERROR
        assert code != EXIT_MISMATCH
        assert "error:" in capsys.readouterr().out


def test_the_cycle_is_priced_from_netted_meter_registers() -> None:
    """`netted` silences the "intervals carry both directions" warning.

    Every solar cycle raises it once its readings are aggregated to an hour,
    and the audit's sources are all a meter's own netted registers.
    """
    import inspect

    from audit import cli

    source = inspect.getsource(cli._reconcile)
    assert "compute_segments(segments, readings, netted=True)" in source


class _Portal:
    def __init__(self) -> None:
        self.downloads = 0

    def download_bill(self, bill_id: str) -> bytes:
        self.downloads += 1
        return b"%PDF-1.4 stand-in"


class TestDownloadedStatements:
    REF = BillRef(bill_id="b1", statement_date=date(2026, 10, 5), printed_amount=62.39)

    def test_a_statement_stays_on_disk_and_is_not_fetched_twice(self, tmp_path: Path) -> None:
        portal = _Portal()
        for _ in range(2):
            with downloaded(portal, self.REF, cache=tmp_path, keep=True) as path:
                assert path.is_file()
        assert (tmp_path / statement_name(self.REF)).is_file()
        assert portal.downloads == 1

    def test_discarding_removes_it_after_the_run(self, tmp_path: Path) -> None:
        with downloaded(_Portal(), self.REF, cache=tmp_path, keep=False) as path:
            assert path.is_file()
        assert not path.exists()

    def test_a_statement_is_readable_only_by_its_owner(self, tmp_path: Path) -> None:
        cache = statement_cache(tmp_path / "statements", "someone|1234")
        with downloaded(_Portal(), self.REF, cache=cache, keep=True) as path:
            assert path.stat().st_mode & 0o777 == 0o600
        assert cache.stat().st_mode & 0o777 == 0o700
        assert cache.parent.stat().st_mode & 0o777 == 0o700

    def test_a_copy_kept_before_the_cache_was_private_is_made_private(self, tmp_path: Path) -> None:
        older = tmp_path / statement_name(self.REF)
        older.write_bytes(b"%PDF-1.4 stand-in")
        older.chmod(0o644)
        with downloaded(_Portal(), self.REF, cache=tmp_path, keep=True) as path:
            assert path.stat().st_mode & 0o777 == 0o600

    def test_two_logins_never_share_a_statement(self, tmp_path: Path) -> None:
        # Both statements are named for the day they were issued; billed the
        # same day, one account would reconcile against the other's copy.
        mine = statement_cache(tmp_path, "someone|1234")
        theirs = statement_cache(tmp_path, "someone-else|5678")
        assert mine != theirs
        assert "1234" not in mine.name
        portal = _Portal()
        with downloaded(portal, self.REF, cache=mine, keep=True):
            pass
        with downloaded(portal, self.REF, cache=theirs, keep=True):
            pass
        assert portal.downloads == 2

    def test_statements_listed_alike_each_get_a_fresh_copy(self) -> None:
        twin = BillRef(bill_id="b2", statement_date=self.REF.statement_date, printed_amount=62.39)
        corrected = BillRef(
            bill_id="b3", statement_date=self.REF.statement_date, printed_amount=60.0
        )
        assert statement_names([self.REF, twin, corrected]) == [
            ("PGE_20261005_62.39-1.pdf", False),
            ("PGE_20261005_62.39-2.pdf", False),
            ("PGE_20261005_60.00.pdf", True),
        ]

    def test_a_statement_corrected_between_runs_is_not_read_from_the_old_copy(
        self, tmp_path: Path
    ) -> None:
        corrected = BillRef(
            bill_id="b9", statement_date=self.REF.statement_date, printed_amount=60.0
        )
        portal = _Portal()
        for ref in (self.REF, corrected):
            ((name, reuse),) = statement_names([ref])
            with downloaded(portal, ref, cache=tmp_path, keep=True, name=name, reuse=reuse):
                pass
        assert portal.downloads == 2
        assert (tmp_path / statement_name(corrected)).is_file()

    def test_a_copy_that_cannot_be_trusted_is_fetched_again(self, tmp_path: Path) -> None:
        portal = _Portal()
        for _ in range(2):
            with downloaded(portal, self.REF, cache=tmp_path, keep=True, reuse=False):
                pass
        assert portal.downloads == 2
