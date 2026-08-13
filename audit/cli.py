"""Command line for the audit harness.

Mirrors ``nem_rates.cli``'s conventions -- argparse subcommands dispatched by
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
from pathlib import Path

from nem_rates import __version__ as library_version

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
        version=f"audit {__version__} (nem-rates {library_version})",
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
    return parser


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
            )
    except AuditError as exc:
        print(f"error: {exc}")
        return EXIT_ERROR

    parser.print_help()
    return EXIT_OK


def _reconcile(
    paths: Sequence[Path],
    *,
    account: Path,
    read_hour: int,
    verbose: bool,
    as_json: bool,
) -> int:
    from nem_rates.billing import BillEngine
    from nem_rates.engine import RateEngine
    from nem_rates.sources.influx import InfluxSettings, read_counters

    from .account import AccountHistory, check_against_statement
    from .reconcile import reconcile, render_all
    from .sources import compare_sources, window
    from .statements import read_statement

    history = AccountHistory.from_toml(account)
    settings = InfluxSettings.load()

    results = []
    worst = EXIT_OK
    for path in paths:
        statement = read_statement(path)

        # A parse that does not add up cannot be compared against anything: the
        # difference would be reported as a billing defect when it is a reading
        # defect, which is the one thing that would make this harness worthless.
        problems = statement.self_check()
        if problems:
            print(f"{path.name}: the statement did not survive its own checks")
            for problem in problems:
                print(f"  {problem}")
            worst = EXIT_ERROR
            continue

        config = history.config_for(statement.period)
        stale = check_against_statement(config, statement)
        if stale:
            print(f"{path.name}: the configured account does not describe this statement")
            for problem in stale:
                print(f"  {problem}")
            worst = EXIT_ERROR
            continue

        start, end = window(statement.period, read_hour=read_hour)
        readings = read_counters(settings, start, end)
        bill = BillEngine(RateEngine(config)).compute(readings, statement.period)
        results.append(
            reconcile(
                statement,
                bill,
                config,
                source_deltas=compare_sources({"influx": readings}, statement),
            )
        )

    if as_json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    elif results:
        print(render_all(results, verbose=verbose))

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
