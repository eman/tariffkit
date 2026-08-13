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
from collections.abc import Sequence

from nem_rates import __version__ as library_version

from . import __version__

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
    # Subcommands land with their milestones. `required=False` until then, so
    # `--version` works on its own rather than failing with a usage error that
    # names commands which do not exist yet.
    parser.add_subparsers(dest="command", required=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # Nothing to dispatch to yet. The parser exists so that --version and
    # --help work and the packaging wiring is proven end to end before any
    # behaviour depends on it.
    parser.print_help()
    return EXIT_OK
