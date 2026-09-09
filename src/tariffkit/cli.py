"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from . import __version__
from .config import Config
from .engine import RateEngine
from .errors import ConfigError, TariffKitError
from .models import PriceCurve, PricePoint
from .secrets import (
    SECRET_NAMES,
    configured_named_secrets,
    configured_secrets,
    delete_named_secret,
    delete_secret,
    set_named_secret,
    set_secret,
)
from .timeutil import PACIFIC, to_pacific


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tariffkit",
        description="PG&E E-ELEC import/export prices under NEM 3.0.",
    )
    parser.add_argument("--version", action="version", version=f"tariffkit {__version__}")
    parser.add_argument("--config", type=Path, help="path to a config TOML file")
    parser.add_argument("--account", help="named account profile to use")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    credentials = sub.add_parser(
        "credentials",
        help="store credentials in the operating-system keyring",
    )
    credential_commands = credentials.add_subparsers(dest="credential_command", required=True)
    credential_set = credential_commands.add_parser("set", help="prompt for and store a secret")
    credential_set.add_argument("--set", dest="credential_set", metavar="NAME")
    credential_set.add_argument("name", choices=SECRET_NAMES)
    credential_delete = credential_commands.add_parser("delete", help="delete a stored secret")
    credential_delete.add_argument("--set", dest="credential_set", metavar="NAME")
    credential_delete.add_argument("name", choices=SECRET_NAMES)
    credential_list = credential_commands.add_parser(
        "list", help="list configured names without values"
    )
    credential_list.add_argument("--set", dest="credential_set", metavar="NAME")

    account = sub.add_parser("account", help="manage named account profiles")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    account_init = account_commands.add_parser("init", help="create a named account profile")
    account_init.add_argument("name")
    account_init.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_init.add_argument("--effective", type=date.fromisoformat)
    account_init.add_argument("--config-json", type=Path)
    account_init.add_argument("--credential-set")
    account_init.add_argument("--audit-file", type=Path)
    account_init.add_argument("--json", action="store_true")
    account_commands.add_parser("list", help="list named account profiles").add_argument(
        "--json", action="store_true"
    )
    account_show = account_commands.add_parser("show", help="show a named account profile")
    account_show.add_argument("name")
    account_show.add_argument("--json", action="store_true")
    account_history = account_commands.add_parser(
        "history", help="show account epochs and evidence"
    )
    account_history.add_argument("name")
    account_history.add_argument("--json", action="store_true")
    account_update = account_commands.add_parser("update", help="add or replace an account epoch")
    account_update.add_argument("name")
    account_update.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_update.add_argument("--effective", required=True, type=date.fromisoformat)
    account_update.add_argument("--config-json", type=Path)
    account_update.add_argument("--credential-set")
    account_update.add_argument("--tariff")
    account_update.add_argument("--supplier")
    account_update.add_argument("--interconnection-year", type=int, dest="interconnection_year")
    account_update.add_argument("--pto-date", type=date.fromisoformat)
    account_update.add_argument("--vintage")
    account_update.add_argument("--acc-plus-segment", dest="acc_plus_segment")
    account_update.add_argument("--discount")
    account_update.add_argument("--base-services-charge-tier", type=int)
    account_update.add_argument("--baseline-territory", dest="baseline_territory")
    account_update.add_argument("--baseline-code", dest="baseline_code")
    account_update.add_argument("--nsc-rate", type=float, dest="nsc_rate")
    account_update.add_argument("--cca-json")
    account_update.add_argument("--note")
    account_update.add_argument("--apply", action="store_true")
    account_update.add_argument("--json", action="store_true")
    account_import = account_commands.add_parser(
        "import-statement", help="import one or more local PG&E statement PDFs"
    )
    account_import.add_argument("name")
    account_import.add_argument("pdf", nargs="+", type=Path)
    account_import.add_argument("--apply", action="store_true")
    account_import.add_argument("--json", action="store_true")
    account_sync = account_commands.add_parser("sync", help="sync statements from the PG&E portal")
    account_sync.add_argument("name")
    account_sync.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_sync.add_argument("--since", type=date.fromisoformat)
    account_sync.add_argument("--apply", action="store_true")
    account_sync.add_argument("--keep-statements", action="store_true")
    account_sync.add_argument("--json", action="store_true")
    account_export = account_commands.add_parser(
        "export", help="export a sanitized account profile"
    )
    account_export.add_argument("name")
    account_export.add_argument("--output", type=Path)
    account_export.add_argument("--json", action="store_true")
    account_source = account_commands.add_parser(
        "source", help="manage profile-scoped grid meter entities"
    )
    account_source.add_argument("name")
    source_commands = account_source.add_subparsers(dest="source_command", required=True)
    source_show = source_commands.add_parser("show", help="show one provider's meter entities")
    source_show.add_argument("provider", choices=("ha", "influx"))
    source_show.add_argument("--json", action="store_true")
    source_set = source_commands.add_parser("set", help="set one provider's meter entities")
    source_set.add_argument("provider", choices=("ha", "influx"))
    source_set.add_argument(
        "--grid-import-entity",
        "--import-entity",
        dest="grid_import_entity",
        required=True,
        help="entity measuring energy consumed from the grid",
    )
    source_set.add_argument(
        "--grid-export-entity",
        "--export-entity",
        dest="grid_export_entity",
        required=True,
        help="entity measuring energy exported to the grid",
    )
    source_set.add_argument("--apply", action="store_true")
    source_set.add_argument("--json", action="store_true")

    now = sub.add_parser("now", help="current import and export price")
    now.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    now.add_argument("--account", default=argparse.SUPPRESS, help="named account profile to use")
    now.add_argument("--json", action="store_true", help="emit JSON")

    forecast = sub.add_parser("forecast", help="upcoming hourly prices")
    forecast.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    forecast.add_argument(
        "--account", default=argparse.SUPPRESS, help="named account profile to use"
    )
    forecast.add_argument("--hours", type=int, default=24)
    forecast.add_argument("--start", type=datetime.fromisoformat, help="ISO 8601 with offset")
    forecast.add_argument("--format", choices=("table", "json", "csv"), default="table")

    info_parser = sub.add_parser("info", help="which data is loaded, and from where")
    info_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    info_parser.add_argument(
        "--account", default=argparse.SUPPRESS, help="named account profile to use"
    )

    bill = sub.add_parser("bill", help="compute a bill from interval meter data")
    bill.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    bill.add_argument(
        "--account",
        default=argparse.SUPPRESS,
        help="named account profile to price from. A profile is a dated history of the "
        "agreement -- every tariff it has been on and when, who supplies generation, the "
        "Permission To Operate date, and which meter entities to read -- where --config "
        "and config.toml describe a single moment. Without one, a cycle crossing a rate "
        "change is priced at one tariff throughout and a CCA account is priced as bundled, "
        "which gives it one export credit bank where it has two. Defaults to the profile "
        "named in config.toml, if any; `tariffkit account list` shows them",
    )
    bill.add_argument(
        "csv",
        type=Path,
        nargs="?",
        metavar="GREEN_BUTTON_CSV",
        help="PG&E Green Button CSV ('Download my data'); '-' for stdin. "
        "Omit with --source ha or --source influx",
    )
    bill.add_argument(
        "--source",
        # "csv" stays accepted so existing invocations keep working, but it is
        # not the documented spelling: it says nothing about which CSV.
        choices=("green-button", "csv", "ha", "influx"),
        default="green-button",
        help="where the readings come from (default: green-button)",
    )
    bill.add_argument("--start", type=date.fromisoformat, help="cycle start (meter read date)")
    bill.add_argument("--end", type=date.fromisoformat, help="cycle end, inclusive")
    bill.add_argument("--json", action="store_true")
    bill.add_argument("--no-check", dest="check", action="store_false", help="skip coverage checks")
    bill.add_argument("--ha-import-entity", help="override the grid-import entity")
    bill.add_argument("--ha-export-entity", help="override the grid-export entity")
    bill.add_argument("--influx-import-entity", help="override the grid-import series")
    bill.add_argument("--influx-export-entity", help="override the grid-export series")
    bill.add_argument(
        "--influx-resolution",
        type=int,
        default=60,
        metavar="MINUTES",
        help="interval length for InfluxDB counters (default: 60; finer needs dense sampling)",
    )
    bill.add_argument(
        "--ha-resolution",
        choices=("auto", "5minute", "hour"),
        default="auto",
        help="statistics resolution; auto prefers 5-minute where it still exists",
    )

    mqtt = sub.add_parser("mqtt", help="publish to MQTT every hour")
    mqtt.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    mqtt.add_argument("--account", default=argparse.SUPPRESS, help="named account profile to use")
    mqtt.add_argument("--broker")
    mqtt.add_argument("--port", type=int)
    mqtt.add_argument("--username")
    mqtt.add_argument("--topic-prefix")
    mqtt.add_argument("--forecast-hours", type=int)
    mqtt.add_argument("--tls", action="store_true", default=None)
    mqtt.add_argument(
        "--allow-insecure-auth",
        action="store_true",
        default=None,
        help="allow MQTT credentials without TLS on an isolated trusted network",
    )
    mqtt.add_argument(
        "--no-discovery",
        dest="discovery",
        action="store_false",
        default=None,
        help="skip Home Assistant discovery config",
    )
    mqtt.add_argument("--once", action="store_true", help="publish once and exit")

    serve = sub.add_parser("serve", help="run the REST API")
    serve.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    serve.add_argument("--account", default=argparse.SUPPRESS, help="named account profile to use")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    return parser


def _midnight(day: date) -> datetime:
    """Local midnight starting ``day`` -- where a billing cycle boundary falls.

    Callers add ``timedelta(days=1)`` to get the end of a cycle, and that is
    deliberately wall-clock arithmetic: a cycle closes at the next local
    midnight, 23 real hours later across the spring transition and 25 across the
    autumn one. Converting to absolute time first would hold the window at 24
    hours and land it an hour off on those two days -- the opposite of what
    coverage checking needs, where elapsed time is the right measure.
    """
    return datetime(day.year, day.month, day.day, tzinfo=PACIFIC)


def _format_point(point: PricePoint) -> str:
    lines = [
        # %Z on both ends: across the fall-back transition the two sides carry
        # different offsets, and "01:00 PDT - 01:00" reads as a zero-length hour.
        f"{point.start:%Y-%m-%d %H:%M %Z} - {point.end:%H:%M %Z}",
        f"  import  {point.import_price.total:>9.5f} $/kWh"
        f"   ({point.import_price.season}/{point.import_price.period})",
        f"  export  {point.export_price.total:>9.5f} $/kWh"
        f"   ({point.export_price.vintage}/{point.export_price.day_type})",
        f"  spread  {point.spread:>+9.5f} $/kWh",
    ]
    if point.spread > 0:
        lines.append("  exporting beats self-consumption this hour")
    for note in _caveats(point):
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _caveats(point: PricePoint) -> list[str]:
    notes = []
    if not point.export_price.locked:
        notes.append("past the 9-year rate lock; PG&E publishes this for illustration only")
    if not point.export_price.exact:
        notes.append("upstream hour labels drift this far out; value may be off by one hour slot")
    if not point.export_price.complete:
        notes.append("delivery only; configure your CCA's export rate for a full credit")
    if not point.import_price.complete:
        notes.append("delivery only; configure your CCA's generation rate card")
    return notes


def _write_csv(curve: PriceCurve, stream: Any) -> None:
    writer = csv.writer(stream)
    writer.writerow(["start", "end", "import", "export", "spread", "season", "period", "locked"])
    for point in curve:
        writer.writerow(
            [
                point.start.isoformat(),
                point.end.isoformat(),
                f"{point.import_price.total:.5f}",
                f"{point.export_price.total:.5f}",
                f"{point.spread:.5f}",
                point.import_price.season,
                point.import_price.period,
                point.export_price.locked,
            ]
        )


def _print_table(curve: PriceCurve) -> None:
    print(f"{'hour':<17} {'import':>9} {'export':>9} {'spread':>9}  period")
    print("-" * 62)
    best = max(p.export_price.total for p in curve)
    for point in curve:
        marker = " *" if point.export_price.total == best else ""
        print(
            f"{point.start:%Y-%m-%d %H:%M}  "
            f"{point.import_price.total:>9.5f} {point.export_price.total:>9.5f} "
            f"{point.spread:>+9.5f}  {point.import_price.period}{marker}"
        )
    print(f"\n* highest export credit in this window ({best:.5f} $/kWh)")


def _print_banks(entry: Any, config: Any) -> None:
    """The export credit bank, laid out the way a statement lays it out.

    Four columns, because one number cannot say what happened: a statement
    prints Beginning Balance, Earned This Period, Applied This Period and
    Remaining Balance, and the interesting thing about a heavy-export cycle is
    that the second and the third are nothing like each other.

    And one bank per supplier, never their sum. Where a Community Choice
    Aggregator supplies generation there are two, on unrelated settlement
    calendars -- PG&E's at the Permission To Operate anniversary, the CCA's on
    its own cash-out year -- and adding them gives a figure no statement shows
    and that never settles as a whole. ``CreditBalances.held_by`` exists to keep
    them apart and printing ``closing.total`` ignored it, which on a real CCA
    cycle announced a single "+94.43" where the statement prints $11.96 on one
    page and $98.79 on another.

    Opening is zero here and says so: this command prices one cycle on its own,
    so nothing carries in. A bank that accumulates needs the run of cycles
    before it, which is what ``run_ledger`` and the Home Assistant bank entity
    are for.
    """
    from .models import Supplier

    if not (entry.earned.total or entry.opening.total):
        return
    supplier = getattr(config, "supplier", None)
    cca = getattr(config, "cca", None)
    split = supplier is Supplier.CCA
    utility = getattr(getattr(config, "utility", None), "short_name", "utility")
    banks = (
        [
            (f"{utility} (delivery, bonus)", "utility"),
            (f"{cca.name if cca else 'CCA'} (generation, bonus)", "generation"),
        ]
        if split
        else [(f"{utility} (all buckets)", "utility")]
    )

    print("\nexport credit bank")
    print(f"  {'':<30} {'opening':>9} {'earned':>9} {'applied':>9} {'remaining':>9}")
    for label, party in banks:
        print(
            f"  {label:<30} "
            f"{entry.opening.held_by(party, split=split):>9.2f} "
            f"{entry.earned.held_by(party, split=split):>9.2f} "
            f"{entry.applied.held_by(party, split=split):>9.2f} "
            f"{entry.closing.held_by(party, split=split):>9.2f}"
        )


def _priced_as(config: Any, profile_name: str | None) -> str:
    """Which arrangement the bill was priced under.

    Worth a line because the answer changes the shape of the output and there
    was no way to read it off. A bundled account has one export credit bank and
    a CCA account has two, so a customer of a CCA who priced without naming
    their profile saw a single bank and no sign of why -- the arrangement lives
    in the account profile, and a plain config.toml says "bundled" by default.
    """
    from .models import Supplier

    tariff = getattr(config, "tariff", "?")
    cca = getattr(config, "cca", None)
    utility = getattr(getattr(config, "utility", None), "short_name", "the utility")
    by = cca.name if getattr(config, "supplier", None) is Supplier.CCA and cca else utility
    source = (
        f'account profile "{profile_name}"'
        if profile_name
        else "config.toml -- no account profile selected, so --account may be missing"
    )
    return f"priced from {source}: {tariff}, generation by {by}"


def _print_bill(bill: Any, config: Any = None, profile_name: str | None = None) -> None:
    p = bill.period
    print(f"Billing period {p.start} to {p.end} ({p.days} days)")
    if config is not None:
        print(f"{_priced_as(config, profile_name)}\n")
    else:
        print()
    print(f"{'':<11} {'imported':>10} {'$':>8}   {'exported':>10} {'$':>8}")
    print("-" * 54)
    for b in bill.buckets:
        print(
            f"{b.period!s:<11} {b.imported:>10.3f} {b.import_charge:>8.2f}   "
            f"{b.exported:>10.3f} {b.export_credit:>8.2f}"
        )
    print("-" * 54)
    print(
        f"{'totals':<11} {bill.imported_kwh:>10.3f} {bill.energy_charges:>8.2f}   "
        f"{bill.exported_kwh:>10.3f} {bill.export_credits:>8.2f}"
    )

    print("\ncharges")
    for name, amount in sorted(bill.import_components.items(), key=lambda kv: -abs(kv[1])):
        print(f"  {name:<34} {amount:>+9.2f}")
    print("\ncredits")
    for name, amount in sorted(bill.export_components.items(), key=lambda kv: -abs(kv[1])):
        print(f"  {name:<34} {amount:>+9.2f}")
    print("\nfixed")
    for name, amount in bill.fixed_components.items():
        print(f"  {name:<34} {amount:>+9.2f}")

    # What a statement would charge, not `Bill.total`.
    #
    # `Bill.total` subtracts every export credit from every charge, and the
    # tariff does not allow that: credits are scoped, and credit beyond what its
    # own bucket can absorb banks rather than reducing the bill. On an exporting
    # account the two are nowhere near each other and only one of them appears
    # on a statement. Printing the wrong one under the word TOTAL invited the
    # obvious reading -- a 2026-07-29..08-27 cycle printed -73.36 against a
    # statement whose electric charges were 14.22, and the figure matched
    # nothing on the page. The right one, on the same cycle, is 14.20.
    #
    # Shown as the statement lays it out, because the point of the three lines
    # is that the third is not the first two subtracted: what the credit could
    # not reach is banked, not owed.
    from .billing import apply_credits

    entry = apply_credits(bill)
    print(f"\n  {'gross charges':<34} {entry.gross_charges:>+9.2f}")
    print(f"  {'credit applied':<34} {-entry.applied.total:>+9.2f}")
    print(f"  {'AMOUNT DUE':<34} {entry.cash_due:>+9.2f}")
    _print_banks(entry, config)
    if bill.effective_import_rate:
        print(f"  {'effective $/kWh imported':<34} {bill.effective_import_rate:>9.5f}")
    for warning in bill.warnings:
        print(f"\n  warning: {warning}")
    if not bill.complete:
        print("  note: some prices were incomplete or inexact; treat the total as an estimate")


def _account_store() -> Any:
    from .account import AccountStore

    return AccountStore()


def _pricing_context(args: Any) -> tuple[Any, Config | None, bool, Any | None]:
    """Price from the account where there is one, and from a Config otherwise.

    The account wins unless ``--config`` names a file explicitly, because a
    bill has to price with the settings in force over its own days and only the
    account carries that history. A stateless Config remains the answer for
    someone who has not set an account up, and for anyone deliberately pricing a
    hypothetical.
    """
    from .account import AccountRateEngine

    store = _account_store()
    if getattr(args, "config", None) is None and store.exists():
        return AccountRateEngine(store.load()), None, True, store
    config = Config.load(getattr(args, "config", None))
    return RateEngine(config), config, False, None


def _print_profile(profile: Any, *, json_output: bool) -> None:
    from .account.cli import profile_summary

    if json_output:
        print(json.dumps(profile.to_dict(), indent=2, default=str))
        return
    summary = profile_summary(profile)
    print("epochs")
    for epoch in summary["epochs"]:
        print(
            f"  {epoch['effective']}  {epoch['tariff']} / {epoch['supplier']}"
            + (f"  {epoch['note']}" if epoch["note"] else "")
        )
    print(f"observations: {summary['observations']}")


def _print_skipped(skipped: Sequence[Mapping[str, str]]) -> None:
    """Name the statements that were not imported, without failing the run.

    On stderr and prefixed "skipped", not "error": the command did its job for
    everything else, and printing this as a failure is what sent an owner
    looking for a broken portal session when one document out of twenty-five
    was simply not a statement this parser recognises.
    """
    for entry in skipped:
        print(f"skipped {entry['statement']}: {entry['reason']}", file=sys.stderr)


def _run_account_command(args: Any) -> int:
    from .account.cli import (
        config_changes,
        import_statements,
        init_profile,
        sync_profile,
        update_profile,
    )

    store = _account_store()
    command = args.account_command
    if command == "init":
        profile = init_profile(
            store,
            config_path=args.config,
            config_json=args.config_json,
            effective=args.effective,
            audit_path=args.audit_file,
        )
        _print_profile(profile, json_output=args.json)
        return 0

    profile = store.load()
    if command == "show":
        _print_profile(profile, json_output=args.json)
        return 0

    if command == "history":
        if args.json:
            print(json.dumps(profile.to_dict(), indent=2, default=str))
        else:
            _print_profile(profile, json_output=False)
            for index, observation in enumerate(profile.observations, start=1):
                agreements = ", ".join(
                    f"{agreement.period.start}..{agreement.period.end} "
                    f"{agreement.tariff or 'unknown'}"
                    for agreement in observation.agreements
                )
                print(f"evidence {index}: {agreements}")
        return 0

    if command == "update":
        changes = config_changes(args)
        updated = update_profile(
            store,
            effective=args.effective,
            config_path=args.config,
            config_json=args.config_json,
            changes=changes,
            note=args.note,
            apply=args.apply,
        )
        if args.json:
            print(
                json.dumps(
                    {"profile": updated.to_dict(), "applied": args.apply},
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"updated {args.name} at {args.effective}")
            if not args.apply:
                print("preview only; pass --apply to save")
        return 0

    if command == "import-statement":
        _updated, proposals, skipped = import_statements(
            store,
            args.pdf,
            apply=args.apply,
        )
        payload = {
            "profile": args.name,
            "applied": args.apply,
            "proposals": proposals,
            "skipped": skipped,
        }
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            # Only the files that produced one. `strict=True` is the right
            # setting and would now raise, because a skipped statement has no
            # proposal to pair with.
            refused = {entry["statement"] for entry in skipped}
            imported = [path for path in args.pdf if str(path) not in refused]
            _print_skipped(skipped)
            for path, proposal in zip(imported, proposals, strict=True):
                print(f"{path.name}:")
                proposal_changes = cast(list[dict[str, Any]], proposal["changes"])
                if proposal_changes:
                    for change in proposal_changes:
                        print(
                            f"  {change['outcome'].upper()} {change['effective']} {change['field']}"
                        )
                else:
                    print("  no account changes")
            if not args.apply:
                print("preview only; pass --apply to save")
        return 0

    if command == "sync":
        _updated, proposals, skipped = sync_profile(
            store,
            since=args.since,
            apply=args.apply,
            keep_statements=args.keep_statements,
            config_path=args.config,
        )
        payload = {
            "profile": args.name,
            "applied": args.apply,
            "proposals": proposals,
            "skipped": skipped,
        }
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            _print_skipped(skipped)
            print(f"received {len(proposals)} statement update(s)")
            for proposal in proposals:
                proposal_changes = cast(list[dict[str, Any]], proposal["changes"])
                print(f"  {len(proposal_changes)} change(s)" + ("" if args.apply else " (preview)"))
            if not args.apply:
                print("preview only; pass --apply to save")
        return 0

    if command == "export":
        raw = json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output is None:
            print(raw, end="")
        else:
            args.output.write_text(raw, encoding="utf-8")
            args.output.chmod(0o600)
            if args.json:
                print(json.dumps({"profile": args.name, "output": str(args.output)}))
        return 0

    if command == "source":
        from .account.cli import meter_source_summary, set_meter_source

        if args.source_command == "show":
            summary = meter_source_summary(profile, args.provider)
            if args.json:
                print(json.dumps(summary, indent=2))
            elif summary["configured"]:
                print(f"profile: {summary['profile']}")
                print(f"source: {summary['source']}")
                print(f"grid import: {summary['grid_import_entity']}")
                print(f"grid export: {summary['grid_export_entity']}")
            else:
                print(f"profile: {summary['profile']}")
                print(f"source: {summary['source']}")
                print("not configured; the source default will be used")
            return 0

        updated = set_meter_source(
            store,
            provider=args.provider,
            grid_import_entity=args.grid_import_entity,
            grid_export_entity=args.grid_export_entity,
            apply=args.apply,
        )
        summary = meter_source_summary(updated, args.provider)
        payload = {**summary, "applied": args.apply}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"profile: {args.name}")
            print(f"source: {args.provider}")
            print(f"grid import: {summary['grid_import_entity']}")
            print(f"grid export: {summary['grid_export_entity']}")
            if not args.apply:
                print("preview only; pass --apply to save")
        return 0

    raise AssertionError(f"unhandled account command {command}")


def _mqtt_settings(args: Any, *, config: Config | None, profile_name: str | None) -> Any:
    """Build MQTT settings, keeping ``--config``'s stateless choice authoritative.

    ``MqttSettings.load`` independently falls back to a configured default
    profile (from the config file's ``[account]`` table or the
    ``TARIFFKIT_ACCOUNT``/``TARIFFKIT_PROFILE`` environment variables) whenever
    it is not told a profile explicitly. That fallback is correct for callers
    of ``MqttSettings.load`` directly, but here ``_pricing_context`` already
    resolved precedence: a non-``None`` ``config`` means the caller chose
    ``--config`` and no profile should apply, even if one is configured
    elsewhere. ``profile=None`` is indistinguishable from "not specified" once
    it reaches ``load``'s override handling, so that decision has to be
    re-asserted here instead.
    """
    from dataclasses import replace

    from .mqtt import MqttSettings

    settings = MqttSettings.load(
        config_path=args.config,
        broker=args.broker,
        port=args.port,
        username=args.username,
        topic_prefix=args.topic_prefix,
        discovery=args.discovery,
        forecast_hours=args.forecast_hours,
        tls=args.tls,
        allow_insecure_auth=args.allow_insecure_auth,
        profile=profile_name,
    )
    if config is not None and settings.profile is not None:
        settings = replace(settings, profile=None)
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        if args.command == "credentials":
            if args.credential_command == "set":
                value = getpass.getpass(f"{args.name}: ")
                if args.credential_set:
                    set_named_secret(args.credential_set, args.name, value)
                else:
                    set_secret(args.name, value)
                print(f"stored {args.name}")
            elif args.credential_command == "delete":
                if args.credential_set:
                    delete_named_secret(args.credential_set, args.name)
                else:
                    delete_secret(args.name)
                print(f"deleted {args.name}")
            else:
                names = (
                    configured_named_secrets(args.credential_set)
                    if args.credential_set
                    else configured_secrets()
                )
                for name in names:
                    print(name)
            return 0

        if args.command == "account":
            return _run_account_command(args)

        engine, config, from_account, profile_store = _pricing_context(args)

        if args.command == "now":
            point = engine.price_now()
            print(json.dumps(point.to_dict(), indent=2) if args.json else _format_point(point))
            return 0

        if args.command == "forecast":
            start = to_pacific(args.start) if args.start else None
            curve = engine.forecast(hours=args.hours, start=start)
            if args.format == "json":
                print(json.dumps(curve.to_dict(), indent=2))
            elif args.format == "csv":
                _write_csv(curve, sys.stdout)
            else:
                _print_table(curve)
            return 0

        if args.command == "info":
            info = dict(engine.describe())
            if hasattr(engine, "export_rates"):
                info["exact_through"] = engine.export_rates.exact_through
            info["daily_fixed_charge"] = engine.daily_fixed_charge()
            print(json.dumps(info, indent=2, default=str))
            return 0

        if args.command == "bill":
            from .billing import BillEngine, BillingPeriod
            from .sources import read_green_button

            account_profile = None
            if from_account:
                from .account import AccountRateEngine

                if not isinstance(engine, AccountRateEngine):
                    raise AssertionError("selected account did not produce an account rate engine")
                account_profile = engine.profile

            note = ""
            if args.source == "ha":
                from .sources import HaSettings, describe_resolution, read_statistics

                if not (args.start and args.end):
                    raise ConfigError("--source ha requires --start and --end")
                ha_settings = HaSettings.load(
                    config_path=args.config,
                    profile_source=(
                        account_profile.meter_sources.ha if account_profile is not None else None
                    ),
                    import_entity=args.ha_import_entity,
                    export_entity=args.ha_export_entity,
                )
                readings = read_statistics(
                    ha_settings,
                    _midnight(args.start),
                    _midnight(args.end) + timedelta(days=1),
                    resolution=args.ha_resolution,
                )
                note = f"  source: Home Assistant statistics ({describe_resolution(readings)})"
            elif args.source == "influx":
                from .sources import InfluxSettings, describe_resolution, read_counters

                if not (args.start and args.end):
                    raise ConfigError("--source influx requires --start and --end")
                influx_settings = InfluxSettings.load(
                    config_path=args.config,
                    profile_source=(
                        account_profile.meter_sources.influx
                        if account_profile is not None
                        else None
                    ),
                    import_entity=args.influx_import_entity,
                    export_entity=args.influx_export_entity,
                )
                step = timedelta(minutes=args.influx_resolution)
                readings = read_counters(
                    influx_settings,
                    _midnight(args.start),
                    _midnight(args.end) + timedelta(days=1),
                    step,
                )
                # Described the same way the Home Assistant source describes
                # itself. The two report the same interval in different words
                # otherwise -- "744 x 60min" against "744 x hour" -- which reads
                # as the sources disagreeing about something when they do not.
                note = (
                    f"  source: InfluxDB counters ({describe_resolution(readings)}; "
                    f"totals are exact, distribution follows sample density)"
                )
            elif args.csv is None:
                raise ConfigError(
                    "give a Green Button CSV path, or use --source ha or --source influx"
                )
            else:
                readings = read_green_button(sys.stdin if str(args.csv) == "-" else args.csv)
                note = f"  source: Green Button CSV ({len(readings)} intervals)"

            period = (
                BillingPeriod(args.start, args.end)
                if args.start and args.end
                else BillingPeriod.from_readings(readings)
            )
            if from_account:
                from .billing.engine import compute_segments

                assert account_profile is not None
                result = compute_segments(
                    account_profile.segments_for(period),
                    readings,
                    check=args.check,
                    # Every source this command reads is a meter's own import and
                    # export registers -- Green Button, Home Assistant
                    # statistics, InfluxDB counters. Both directions inside one
                    # interval is what those registers do once aggregated to an
                    # hour, and reporting it on every solar cycle is noise that
                    # never clears.
                    netted=True,
                )
            else:
                result = BillEngine(engine).compute(readings, period, check=args.check, netted=True)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                # An account profile describes a changing agreement, so the
                # config is the one in force over this cycle rather than a
                # single attribute on the engine.
                _print_bill(
                    result,
                    account_profile.config_at(period.start)
                    if account_profile is not None
                    else engine.config,
                    from_account,
                )
                if note:
                    print(note)
            return 0

        if args.command == "mqtt":
            from .mqtt import MqttPublisher

            settings = _mqtt_settings(args, config=config, from_account=from_account)
            publisher = MqttPublisher(engine, settings)
            if args.once:
                publisher.connect()
                try:
                    publisher.publish_now()
                finally:
                    publisher.close()
            else:
                publisher.run_forever()
            return 0

        if args.command == "serve":
            import uvicorn

            from .web import create_app

            uvicorn.run(
                create_app(
                    config,
                    from_account=from_account,
                    profile_repository=profile_store,
                    config_path=args.config,
                ),
                host=args.host,
                port=args.port,
            )
            return 0

    except TariffKitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130

    # Unreachable: argparse enforces `required=True` on the subparser, so an
    # unknown command exits before we get here.
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
