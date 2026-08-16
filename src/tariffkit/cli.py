"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import sys
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
    bill.add_argument("--account", default=argparse.SUPPRESS, help="named account profile to use")
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


def _print_bill(bill: Any) -> None:
    p = bill.period
    print(f"Billing period {p.start} to {p.end} ({p.days} days)\n")
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

    print(f"\n  {'TOTAL':<34} {bill.total:>+9.2f}")
    if bill.effective_import_rate:
        print(f"  {'effective $/kWh imported':<34} {bill.effective_import_rate:>9.5f}")
    for warning in bill.warnings:
        print(f"\n  warning: {warning}")
    if not bill.complete:
        print("  note: some prices were incomplete or inexact; treat the total as an estimate")


def _account_repository() -> Any:
    from .account import NamedProfileRepository

    return NamedProfileRepository()


def _selected_profile_name(args: Any) -> str | None:
    explicit = cast(str | None, getattr(args, "account", None))
    config_path = getattr(args, "config", None)
    if explicit is not None and config_path is not None:
        raise ConfigError("--account cannot be combined with --config")
    if explicit is not None:
        return explicit
    if config_path is not None:
        return None
    from .account import configured_profile_name

    return configured_profile_name()


def _pricing_context(args: Any) -> tuple[Any, Config | None, str | None, Any | None]:
    """Load either a stateless Config engine or a named profile engine."""
    from .account import AccountRateEngine

    profile_name = _selected_profile_name(args)
    if profile_name is not None:
        repository = _account_repository()
        profile = repository.load(profile_name)
        return AccountRateEngine(profile), None, profile_name, repository
    config = Config.load(args.config)
    return RateEngine(config), config, None, None


def _print_profile(profile: Any, *, json_output: bool) -> None:
    from .account.cli import profile_summary

    if json_output:
        print(json.dumps(profile.to_dict(), indent=2, default=str))
        return
    summary = profile_summary(profile)
    print(f"name: {summary['name']}")
    if summary["credential_set"]:
        print(f"credential set: {summary['credential_set']}")
    print("epochs")
    for epoch in summary["epochs"]:
        print(
            f"  {epoch['effective']}  {epoch['tariff']} / {epoch['supplier']}"
            + (f"  {epoch['note']}" if epoch["note"] else "")
        )
    print(f"observations: {summary['observations']}")


def _run_account_command(args: Any) -> int:
    from .account.cli import (
        config_changes,
        import_statements,
        init_profile,
        sync_profile,
        update_profile,
    )

    repository = _account_repository()
    command = args.account_command
    if command == "list":
        names = repository.names()
        if args.json:
            print(json.dumps(list(names), indent=2))
        else:
            for name in names:
                print(name)
        return 0

    if command == "init":
        profile = init_profile(
            repository,
            args.name,
            config_path=args.config,
            config_json=args.config_json,
            effective=args.effective,
            credential_set=args.credential_set,
            audit_path=args.audit_file,
        )
        _print_profile(profile, json_output=args.json)
        return 0

    profile = repository.load(args.name)
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
            repository,
            args.name,
            effective=args.effective,
            config_path=args.config,
            config_json=args.config_json,
            changes=changes,
            note=args.note,
            credential_set=args.credential_set,
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
        _updated, proposals = import_statements(
            repository,
            args.name,
            args.pdf,
            apply=args.apply,
        )
        payload = {"profile": args.name, "applied": args.apply, "proposals": proposals}
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            for path, proposal in zip(args.pdf, proposals, strict=True):
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
        _updated, proposals = sync_profile(
            repository,
            args.name,
            since=args.since,
            apply=args.apply,
            keep_statements=args.keep_statements,
            config_path=args.config,
        )
        payload = {"profile": args.name, "applied": args.apply, "proposals": proposals}
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
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
            repository,
            args.name,
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

        engine, config, profile_name, profile_repository = _pricing_context(args)

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
            if profile_name is not None:
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
                from .sources import InfluxSettings, read_counters

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
                note = (
                    f"  source: InfluxDB counters ({len(readings)} x {args.influx_resolution}min; "
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
            if profile_name is not None:
                from .billing.engine import compute_segments

                assert account_profile is not None
                result = compute_segments(
                    account_profile.segments_for(period),
                    readings,
                    check=args.check,
                )
            else:
                result = BillEngine(engine).compute(readings, period, check=args.check)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                _print_bill(result)
                if note:
                    print(note)
            return 0

        if args.command == "mqtt":
            from .mqtt import MqttPublisher

            settings = _mqtt_settings(args, config=config, profile_name=profile_name)
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
                    profile_name=profile_name,
                    profile_repository=profile_repository,
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
