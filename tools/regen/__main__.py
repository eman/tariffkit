"""Command line entry point for repository-only rate-data generation."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from tariffkit.errors import ConfigError

from . import DATASETS, run


def _scan_range(raw: str | None) -> tuple[int, int] | None:
    if not raw:
        return None
    lo, separator, hi = raw.partition("-")
    if separator != "-" or not lo.isdigit() or not hi.isdigit():
        raise ConfigError(f"--scan wants a range like 7500-7900, got {raw!r}")
    return int(lo), int(hi)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.regen",
        description="Rebuild vendored rate data from publisher documents.",
    )
    parser.add_argument(
        "dataset",
        nargs="?",
        default="all",
        choices=("all", "tariff", "accplus", "nsc", "cca", "tax"),
    )
    parser.add_argument("--provider")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--for-date", type=date.fromisoformat, metavar="YYYY-MM-DD")
    parser.add_argument("--scan", metavar="LO-HI")
    parser.add_argument("--advice-letter", metavar="NUMBER")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    changed = failed = False
    for dataset in datasets:
        for outcome in run(
            dataset,
            provider=args.provider,
            pdf=args.pdf,
            check=args.check,
            refresh=args.refresh,
            advice_letter=args.advice_letter,
            for_date=args.for_date,
            scan=_scan_range(args.scan),
        ):
            outcome.report()
            changed |= outcome.changed
            failed |= outcome.failed
    if failed:
        return 2
    if args.check and changed:
        print("\nvendored rate data is stale; rerun without --check to update")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
