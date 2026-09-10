"""The command line's contract, which is mostly its exit codes.

The codes are the part other things depend on -- a shell loop, a cron entry, a
human reading a summary -- so they are pinned here rather than left to whatever
the dispatch happens to return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit import __version__
from audit.cli import EXIT_ERROR, EXIT_MISMATCH, EXIT_OK, build_parser, main


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


def test_segment_bills_are_netted_like_the_bill_beside_them() -> None:
    """Both sides of the reconciliation price the same way.

    `netted` only silences the "intervals carry both directions" warning, which
    every solar cycle raises once its readings are aggregated to an hour. Pricing
    the per-segment bills without it put that warning on every segment while the
    merged bill next to them stayed quiet -- the noise the flag exists to stop.
    """
    import inspect

    from audit import cli

    source = inspect.getsource(cli._reconcile)
    assert "price_segments(segments, readings, netted=True)" in source
    assert "compute_segments(segments, readings, netted=True)" in source
