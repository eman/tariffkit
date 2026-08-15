"""Command line for the audit harness.

Mirrors ``tariffkit.cli``'s conventions -- argparse subcommands dispatched by
name, ``--json`` wherever a machine-readable form is useful -- so moving between
the two does not mean learning a second set of habits.

The exit codes are load-bearing:

* ``0`` -- every statement reconciled
* ``1`` -- at least one mismatch, unmapped line, or unmapped component
* ``2`` -- the check could not be performed at all

Two failure codes rather than one because "your numbers disagree" and "I could
not check" need opposite responses from whoever reads them, and a single code
makes a broken harness indistinguishable from a billing error.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from tariffkit import __version__ as library_version

from . import __version__
from .errors import AuditError

#: Every statement reconciled.
EXIT_OK = 0
#: A real disagreement: a mismatch, an unmapped line, or an unmapped component.
EXIT_MISMATCH = 1
#: The audit did not happen. See :mod:`audit.errors`.
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit",
        description="Reconcile computed bills against real PG&E statements.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"audit {__version__} (tariffkit {library_version})",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    read = sub.add_parser("parse", help="read a statement PDF and print what it says")
    read.add_argument("pdf", nargs="+", type=Path)
    read.add_argument("--json", action="store_true", help="machine-readable output")

    check = sub.add_parser("reconcile", help="compare computed bills against statements")
    check.add_argument("pdf", nargs="+", type=Path)
    check.add_argument(
        "--account",
        type=Path,
        default=Path("audit/account.toml"),
        help="the account's dated history (default: audit/account.toml)",
    )
    check.add_argument(
        "--read-hour",
        type=int,
        default=0,
        help="hour of day the meter is read; the cycle boundary is not midnight",
    )
    check.add_argument("--verbose", action="store_true", help="show agreeing lines too")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument(
        "--green-button",
        action="store_true",
        help="also download the utility's own interval export and compare the two meters",
    )

    run = sub.add_parser("run", help="download every statement the portal lists and reconcile it")
    run.add_argument(
        "--since", type=_day, default=None, help="earliest statement date (YYYY-MM-DD)"
    )
    run.add_argument("--until", type=_day, default=None, help="latest statement date (YYYY-MM-DD)")
    run.add_argument(
        "--account",
        type=Path,
        default=Path("audit/account.toml"),
        help="the account's dated history (default: audit/account.toml)",
    )
    run.add_argument("--read-hour", type=int, default=0)
    run.add_argument("--verbose", action="store_true", help="show agreeing lines too")
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.add_argument(
        "--green-button",
        action="store_true",
        help="also download the utility's own interval export and compare the two meters",
    )
    run.add_argument(
        "--keep-statements",
        action="store_true",
        help="leave the downloaded PDFs on disk; they carry the account number and "
        "service address, so they are deleted by default",
    )

    doctor = sub.add_parser(
        "doctor", help="check everything an end-to-end run needs, before running it"
    )
    doctor.add_argument(
        "--account",
        type=Path,
        default=Path("audit/account.toml"),
        help="the account's dated history (default: audit/account.toml)",
    )
    doctor.add_argument("--since", type=_day, default=None, help="oldest cycle you intend to price")
    doctor.add_argument(
        "--offline", action="store_true", help="skip the checks that contact the portal and meter"
    )
    return parser


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as bad:
        raise argparse.ArgumentTypeError(f"expected a date like 2025-10-01, got {text!r}") from bad


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Every AuditError means the check did not happen, and that has to leave a
    # different exit code from "the numbers disagree". Letting one escape gives
    # Python's own exit 1, which reads as a billing discrepancy -- the exact
    # conflation these codes exist to prevent.
    try:
        if args.command == "parse":
            return _parse(args.pdf, as_json=args.json)
        if args.command == "reconcile":
            return _reconcile(
                args.pdf,
                account=args.account,
                read_hour=args.read_hour,
                verbose=args.verbose,
                as_json=args.json,
                green_button=args.green_button,
            )
        if args.command == "run":
            return _run(
                since=args.since,
                until=args.until,
                account=args.account,
                read_hour=args.read_hour,
                verbose=args.verbose,
                as_json=args.json,
                green_button=args.green_button,
                keep=args.keep_statements,
            )
        if args.command == "doctor":
            return _doctor(account=args.account, since=args.since, offline=args.offline)
    except AuditError as exc:
        print(f"error: {exc}")
        return EXIT_ERROR

    parser.print_help()
    return EXIT_OK


def _run(
    *,
    since: date | None,
    until: date | None,
    account: Path,
    read_hour: int,
    verbose: bool,
    as_json: bool,
    green_button: bool,
    keep: bool,
) -> int:
    """Ask the portal what statements exist, then reconcile each of them."""
    from contextlib import ExitStack

    from tariffkit.sources.pge import PgeSession, PgeSettings

    from .run import DEFAULT_CACHE, downloaded, fetch_refs, select

    settings = PgeSettings.load()
    with PgeSession(settings) as session:
        session.login()
        refs = select(fetch_refs(session), since=since, until=until)
        if not refs:
            print("no statements in that range")
            return EXIT_ERROR

        if not as_json:
            span = f"{refs[0].label} to {refs[-1].label}"
            print(f"{len(refs)} statements to check, {span}\n")

        # Downloaded together so the portal work is done and the session can be
        # dropped before any pricing starts -- a slow reconciliation should not
        # be holding an authenticated session open.
        with ExitStack() as stack:
            paths = []
            for ref in refs:
                try:
                    paths.append(
                        stack.enter_context(
                            downloaded(session, ref, cache=DEFAULT_CACHE, keep=keep)
                        )
                    )
                except AuditError as exc:
                    print(f"{ref.label}: {exc}")
            if not paths:
                return EXIT_ERROR
            return _reconcile(
                paths,
                account=account,
                read_hour=read_hour,
                verbose=verbose,
                as_json=as_json,
                green_button=green_button,
            )


def _doctor(*, account: Path, since: date | None = None, offline: bool = False) -> int:
    """Report what an end-to-end run needs and what is missing.

    The first question after any failure is whether the session expired, the
    flow moved, or something was never configured, and those need opposite
    responses. Answering it costs one command rather than a debugging session --
    and reporting every problem at once matters more than it sounds, because
    fixing them one round trip at a time against a live portal is slow.
    """
    from tariffkit.sources.pge import ENDPOINTS

    from .preflight import run_checks

    print("endpoints this client knows:")
    for name, endpoint in sorted(ENDPOINTS.items()):
        mark = "confirmed" if endpoint.captured else "INFERRED"
        print(f"  {mark:>9}  {name:<14} {endpoint.classname}.{endpoint.method}")
    print()

    oldest = since or date.today() - timedelta(days=365)
    checks = run_checks(account=account, oldest=oldest, contact=not offline)
    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"  {check.mark:>8}  {check.name:<{width}}  {check.detail}")

    blocking = [check for check in checks if not check.ok and check.required]
    degraded = [check for check in checks if not check.ok and not check.required]
    print()
    if blocking:
        print(f"{len(blocking)} of {len(checks)} checks block a run")
        return EXIT_ERROR
    if degraded:
        print(f"ready, with reduced coverage: {', '.join(c.name for c in degraded)}")
        return EXIT_OK
    print("ready")
    return EXIT_OK


def _reconcile(
    paths: Sequence[Path],
    *,
    account: Path,
    read_hour: int,
    verbose: bool,
    as_json: bool,
    green_button: bool = False,
) -> int:
    from tariffkit.billing.engine import compute_segments, price_segments
    from tariffkit.engine import RateEngine
    from tariffkit.sources.influx import InfluxSettings, read_counters

    from .account import AccountHistory, check_against_statement
    from .reconcile import reconcile, render_all, render_summary
    from .sources import compare_sources, window
    from .statements import read_statement

    history = AccountHistory.from_toml(account)
    settings = InfluxSettings.load()

    results = []
    skipped: list[str] = []
    worst = EXIT_OK
    for path in paths:
        # Per statement, not per run. PG&E has redesigned the statement at least
        # once inside the window this tool covers, so a batch spanning years
        # will meet a layout the parser does not know -- and letting that abort
        # the run means one old PDF suppresses every check after it.
        try:
            statement = read_statement(path)
        except AuditError as exc:
            print(f"{path.name}: {exc}")
            skipped.append(f"{path.name}: {exc}")
            worst = EXIT_ERROR
            continue

        # A parse that does not add up cannot be compared against anything: the
        # difference would be reported as a billing defect when it is a reading
        # defect, which is the one thing that would make this harness worthless.
        problems = statement.self_check()
        if problems:
            print(f"{path.name}: the statement did not survive its own checks")
            for problem in problems:
                print(f"  {problem}")
            skipped.append(f"{path.name}: {problems[0]}")
            worst = EXIT_ERROR
            continue

        segments = history.segments_for(statement.period)
        config = segments[-1].config
        stale = check_against_statement(config, statement, segments=segments)
        if stale:
            print(f"{path.name}: the configured account does not describe this statement")
            for problem in stale:
                print(f"  {problem}")
            skipped.append(f"{path.name}: {stale[0]}")
            worst = EXIT_ERROR
            continue

        start, end = window(statement.period, read_hour=read_hour)
        readings = read_counters(settings, start, end)
        sources = {"influx": readings}

        if green_button:
            # The utility's own record of the same period. Worth the extra
            # request because one meter cannot tell you it is incomplete: PG&E's
            # export was once missing a whole day, and only a second source
            # showed it.
            from tariffkit.sources.pge import PgeSettings, read_green_button_download

            sources["green_button"] = read_green_button_download(
                PgeSettings.load(), statement.period.start, statement.period.end
            )

        parts = price_segments(segments, readings)
        bill = compute_segments(segments, readings)
        results.append(
            reconcile(
                statement,
                bill,
                config,
                source_deltas=compare_sources(
                    sources, statement, classify=RateEngine(config).tariff.period
                ),
                segment_bills=parts,
            )
        )

    if as_json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    elif results:
        print(render_all(results, verbose=verbose))
        print()
        print(render_summary(results, skipped=skipped))

    if any(not result.ok for result in results):
        worst = max(worst, EXIT_MISMATCH)
    return worst


def _parse(paths: Sequence[Path], *, as_json: bool) -> int:
    from .statements import Section, read_statement

    payload: list[dict[str, object]] = []
    worst = EXIT_OK
    for path in paths:
        try:
            statement = read_statement(path)
        except AuditError as exc:
            print(f"{path.name}: {exc}")
            worst = EXIT_ERROR
            continue

        problems = statement.self_check()
        if problems:
            worst = EXIT_ERROR

        if as_json:
            payload.append(
                {
                    "source": path.name,
                    "statement_date": statement.statement_date.isoformat(),
                    "period": [
                        statement.period.start.isoformat(),
                        statement.period.end.isoformat(),
                    ],
                    "amount_due": statement.amount_due,
                    "sections": {
                        section.name.value: {
                            "printed_total": section.printed_total,
                            "lines": {line.label: line.amount for line in section.lines},
                        }
                        for section in statement.sections
                    },
                    "problems": problems,
                }
            )
            continue

        print(
            f"{path.name}  {statement.period.start}..{statement.period.end} "
            f"({statement.billed_days} days)  due ${statement.amount_due:,.2f}"
        )
        print(
            f"  {statement.rate_schedule or 'unknown schedule'}"
            + (
                f" / {statement.cca_name} {statement.cca_rate_schedule}"
                if statement.cca_name
                else ""
            )
            + (f", baseline {statement.baseline_territory}" if statement.baseline_territory else "")
            + (f", PCIA {statement.pcia_vintage} vintage" if statement.pcia_vintage else "")
        )
        for span in statement.subperiods:
            print(f"  priced in two parts: {span[0]} to {span[1]}")
        for section in statement.sections:
            printed = "" if section.printed_total is None else f"${section.printed_total:,.2f}"
            print(f"  {section.name.value:<16} {len(section.lines):>3} lines  {printed:>12}")
            if section.name is not Section.SUMMARY:
                for line in section.lines:
                    note = " (subtotal)" if line.is_subtotal else ""
                    print(f"      {line.label:<44} {line.amount:>10,.2f}{note}")
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        print()

    if as_json:
        print(json.dumps(payload, indent=2))
    return worst
