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
from typing import TYPE_CHECKING, Any, cast

from .. import __version__
from ..config import Config
from ..engine import RateEngine
from ..errors import ConfigError, TariffKitError
from ..models import PriceCurve, PricePoint
from ..secrets import (
    SECRET_NAMES,
    configured_secrets,
    delete_secret,
    set_secret,
)
from ..timeutil import PACIFIC, to_pacific

if TYPE_CHECKING:
    from ..account import AccountProfile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tariffkit",
        description="PG&E E-ELEC import/export prices under NEM 3.0.",
    )
    parser.add_argument("--version", action="version", version=f"tariffkit {__version__}")
    parser.add_argument("--config", type=Path, help="path to a config TOML file")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    credentials = sub.add_parser(
        "credentials",
        help="store credentials in the operating-system keyring",
    )
    credential_commands = credentials.add_subparsers(dest="credential_command", required=True)
    credential_set = credential_commands.add_parser("set", help="prompt for and store a secret")
    credential_set.add_argument("name", choices=SECRET_NAMES)
    credential_delete = credential_commands.add_parser("delete", help="delete a stored secret")
    credential_delete.add_argument("name", choices=SECRET_NAMES)
    credential_commands.add_parser("list", help="list configured names without values")

    account = sub.add_parser("account", help="manage your account's dated history")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    account_init = account_commands.add_parser("init", help="set up your account")
    account_init.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_init.add_argument("--effective", type=date.fromisoformat)
    account_init.add_argument("--config-json", type=Path)
    account_init.add_argument("--audit-file", type=Path)
    account_init.add_argument("--json", action="store_true")
    account_show = account_commands.add_parser("show", help="show the settings in force today")
    account_show.add_argument("--json", action="store_true")
    account_history = account_commands.add_parser(
        "history", help="show every epoch and the evidence behind it"
    )
    account_history.add_argument("--json", action="store_true")
    account_update = account_commands.add_parser("update", help="add or replace an account epoch")
    account_update.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_update.add_argument("--effective", required=True, type=date.fromisoformat)
    account_update.add_argument("--config-json", type=Path)
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
    account_import.add_argument("pdf", nargs="+", type=Path)
    account_import.add_argument("--apply", action="store_true")
    account_import.add_argument("--json", action="store_true")
    account_sync = account_commands.add_parser("sync", help="sync statements from the PG&E portal")
    account_sync.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_sync.add_argument("--since", type=date.fromisoformat)
    account_sync.add_argument("--apply", action="store_true")
    account_sync.add_argument("--keep-statements", action="store_true")
    account_sync.add_argument("--json", action="store_true")
    account_periods = account_commands.add_parser(
        "periods", help="record the cycle boundaries the utility billed on"
    )
    account_periods.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    account_periods.add_argument("--apply", action="store_true")
    account_periods.add_argument("--json", action="store_true")
    account_export = account_commands.add_parser(
        "export", help="export a sanitized copy of your account"
    )
    account_export.add_argument("--output", type=Path)
    account_export.add_argument("--json", action="store_true")
    account_source = account_commands.add_parser(
        "source", help="manage the grid meter entities to read"
    )
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
    now.add_argument("--json", action="store_true", help="emit JSON")

    forecast = sub.add_parser("forecast", help="upcoming hourly prices")
    forecast.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    forecast.add_argument("--hours", type=int, default=24)
    forecast.add_argument("--start", type=datetime.fromisoformat, help="ISO 8601 with offset")
    forecast.add_argument("--format", choices=("table", "json", "csv"), default="table")

    info_parser = sub.add_parser("info", help="which data is loaded, and from where")
    info_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS)

    bill = sub.add_parser("bill", help="compute a bill from interval meter data")
    bill.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    bill.add_argument(
        "csv",
        type=Path,
        nargs="?",
        metavar="GREEN_BUTTON_CSV",
        help="a Green Button CSV you already have; '-' for stdin. Omit it and "
        "the export is taken from the cache, or downloaded from the portal "
        "and cached. Not used with --source ha or --source influx",
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
    bill.add_argument(
        "--refresh",
        action="store_true",
        help="download the Green Button export again even if it is cached",
    )
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
    from ..models import Supplier

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


def _priced_as(config: Any, from_account: bool) -> str:
    """Which arrangement the bill was priced under.

    Worth a line because the answer changes the shape of the output and there
    was no way to read it off. A bundled account has one export credit bank and
    a CCA account has two, so a customer of a CCA who priced without naming
    their profile saw a single bank and no sign of why -- the arrangement lives
    in the account profile, and a plain config.toml says "bundled" by default.
    """
    from ..models import Supplier

    tariff = getattr(config, "tariff", "?")
    cca = getattr(config, "cca", None)
    utility = getattr(getattr(config, "utility", None), "short_name", "the utility")
    by = cca.name if getattr(config, "supplier", None) is Supplier.CCA and cca else utility
    source = "your account" if from_account else "config.toml -- no account is set up"
    return f"priced from {source}: {tariff}, generation by {by}"


def _print_bill(bill: Any, config: Any = None, from_account: bool = False) -> None:
    p = bill.period
    print(f"Billing period {p.start} to {p.end} ({p.days} days)")
    if config is not None:
        print(f"{_priced_as(config, from_account)}\n")
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
    from ..billing import apply_credits

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
    from .account_store import AccountStore

    return AccountStore()


def _pricing_context(args: Any) -> tuple[Any, Config | None, AccountProfile | None]:
    """Price from the account where there is one, and from a Config otherwise.

    The account wins unless ``--config`` names a file explicitly, because a
    bill has to price with the settings in force over its own days and only the
    account carries that history. A stateless Config remains the answer for
    someone who has not set an account up, and for anyone deliberately pricing a
    hypothetical.

    Reading it happens once, here, and the profile is what every command below
    is handed. Nothing further down goes looking for the file, so `serve` and
    `mqtt` publish the same account this printed.
    """
    from ..account import AccountRateEngine

    named = getattr(args, "config", None)
    if named is None:
        # Only then is the account consulted at all. Asking first meant every
        # priced command touched the configuration directory even when told
        # exactly which file to price from.
        store = _account_store()
        if store.exists():
            profile = store.load()
            return AccountRateEngine(profile), None, profile
    config = Config.load(named)
    return RateEngine(config), config, None


def _print_profile(profile: Any, *, json_output: bool) -> None:
    from .account_commands import profile_summary

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


def _account_state(profile: Any) -> dict[str, Any]:
    """The settings in force today, and where they came from.

    ``show`` used to print the same epoch list as ``history``, which made one
    of the two commands pointless: with no statement evidence recorded they
    were identical output. "What is my account?" is a different question from
    "how did it get here?", and this answers the first one.
    """
    from ..errors import TariffKitError

    today = datetime.now(PACIFIC).date()
    effective: str | None = None
    config: dict[str, Any] | None = None
    try:
        active = profile.config_at(today)
    except TariffKitError:
        # Every epoch is dated in the future -- an account imported ahead of a
        # move-in date. Nothing is in force, which is the answer, not an error.
        pass
    else:
        config = active.to_dict()
        effective = max(
            epoch.effective for epoch in profile.epochs if epoch.effective <= today
        ).isoformat()
    return {
        "effective": effective,
        "config": config,
        "meter_sources": profile.meter_sources.to_dict(),
        "epochs": len(profile.epochs),
        "observations": len(profile.observations),
    }


def _print_account(profile: Any, *, json_output: bool) -> None:
    state = _account_state(profile)
    if json_output:
        print(json.dumps(state, indent=2, default=str))
        return

    if state["config"] is None:
        first = min(epoch.effective for epoch in profile.epochs)
        print(f"nothing in force yet; the first epoch begins {first}")
    else:
        print(f"in force since {state['effective']}")
        rows = _flattened(state["config"])
        width = max(len(key) for key, _ in rows) + 2
        for key, value in rows:
            print(f"  {key:<{width}}{value}")

    sources = state["meter_sources"]
    if any(sources.values()):
        print("\nmeter entities")
        for provider, mapping in sources.items():
            if mapping:
                print(
                    f"  {provider:<9}{mapping['grid_import_entity']} "
                    f"/ {mapping['grid_export_entity']}"
                )

    print(
        f"\n{_plural(state['epochs'], 'epoch')}, "
        f"{_plural(state['observations'], 'statement observation')}"
        " -- see 'tariffkit account history'"
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _flattened(config: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Config as printable rows, with nested tables under a dotted key."""
    rows: list[tuple[str, str]] = []
    for key, value in config.items():
        if isinstance(value, Mapping) and value:
            rows.extend(_flattened(value, f"{prefix}{key}."))
        elif value is None or value == [] or value == {}:
            rows.append((f"{prefix}{key}", "-"))
        elif isinstance(value, bool):
            # The config file is TOML, where these are lowercase.
            rows.append((f"{prefix}{key}", "true" if value else "false"))
        else:
            rows.append((f"{prefix}{key}", str(value)))
    return rows


def _billing_window(args: Any, profile: Any) -> tuple[Any, str]:
    """The cycle to price, defaulting to the one open right now.

    Asking for `--start` and `--end` on every run made the common question --
    "what do I owe so far this cycle?" -- the one thing the command could not
    answer without first looking up when the cycle began.

    Where that boundary came from is returned with it, because it is not always
    known: the utility and its statements fix it exactly, a configured
    meter-read day approximates it, and with none of those the calendar month
    is a guess that will not match a bill. Printing the basis is what keeps the
    last case from reading like the first.
    """
    from ..billing import BillingPeriod, resolve_cycle

    if args.start and args.end:
        return BillingPeriod(args.start, args.end), ""
    if args.start or args.end:
        raise ConfigError("give both --start and --end, or neither for the current cycle")
    if profile is None:
        raise ConfigError(
            "give --start and --end; without an account there is nothing to "
            "say when the current billing cycle began"
        )

    today = datetime.now(PACIFIC).date()
    origins = _known_periods(args, profile, refresh=args.refresh)
    cycle = resolve_cycle(today, _cycle_start_day(args), tuple(origins))
    return BillingPeriod(cycle.start, today), _basis_of(cycle, origins)


def _basis_of(cycle: Any, origins: Mapping[Any, str]) -> str:
    """Which source the boundary actually came from, not which ones exist.

    Deciding this from "does the account hold any statements" labelled a
    portal boundary as a statement's on any account holding one old PDF --
    and on an account whose observations carry no agreement spans at all,
    where every period in play is the portal's.
    """
    if cycle.source != "statement":
        return str(cycle.source)
    for period, origin in origins.items():
        # Either the cycle containing today, or -- cycles being contiguous --
        # the one opening the day after the last period ended.
        if period.start == cycle.start or period.end + timedelta(days=1) == cycle.start:
            return origin
    return "statement"


def _known_periods(args: Any, profile: Any, *, refresh: bool = False) -> dict[Any, str]:
    """Cycle boundaries, from the utility where it will say and the statements otherwise.

    The portal lists every cycle it has billed and the boundaries it billed them
    on -- the same answer a statement carries, without a PDF to parse. It is
    cached, because that changes once a month and pricing from a local meter
    should not start requiring portal credentials.

    Statements still count. An account may have imported them and have no
    credentials configured at all, and the two agree wherever they overlap.

    Returns each period with the source it came from, oldest first, because the
    label printed beside the window has to name whichever one actually answered.
    """
    from ..billing import merge_periods, statement_periods
    from ..sources import PgeSettings, cached_bill_periods

    statements = statement_periods(profile)
    known = merge_periods(statements, profile.billing_periods)
    try:
        settings: Any = PgeSettings.load(config_path=args.config)
    except TariffKitError:
        # No credentials is not an error here: what the account carries still
        # answers, and so does a cached list.
        settings = None
    portal = cached_bill_periods(settings, refresh=refresh)
    # Merged the way the account merges its own two sources: a statement is one
    # page, and the portal lists a bill per agreement, so a cycle split by a
    # mid-cycle change is one period on the statement and two here. Taking the
    # portal's list wholesale opened that cycle on the wrong day and disagreed
    # with what the integration reports for the same account.
    final = merge_periods(known, portal) if portal else known

    # Labelled from the merged list alone. Accumulating labels as the merges
    # went along kept the periods the merges had just dropped, which put a
    # partial statement back beside the whole cycle that supersedes it and
    # handed `resolve_cycle` the partial span again.
    def origin(period: Any) -> str:
        if period in statements:
            return "statement"
        if period in profile.billing_periods:
            return "recorded"
        return "portal"

    return {period: origin(period) for period in final}


#: How each boundary was arrived at, said plainly. The two guesses name what
#: would replace them, because an unbilled cycle is where someone first
#: notices the period does not match their statement.
_BASIS = {
    "portal": "the boundary PG&E billed on",
    "recorded": "the boundary PG&E billed on, recorded on your account",
    "statement": "the boundary your statements print",
    "day_of_month": "from [billing] cycle_start_day; statements would date it exactly",
    "calendar_month": (
        "a calendar month, which is a guess -- store PG&E credentials for the "
        "boundaries the utility billed on, or set [billing] cycle_start_day"
    ),
}


def _cycle_start_day(args: Any) -> int:
    """The meter-read day from ``[billing] cycle_start_day``, if one is set."""
    import tomllib

    from ..config import default_config_path

    path = Path(args.config) if getattr(args, "config", None) else default_config_path()
    if not path.is_file():
        return 0
    raw = tomllib.loads(path.read_text(encoding="utf-8")).get("billing", {})
    day = raw.get("cycle_start_day", 0)
    if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 31:
        raise ConfigError("[billing] cycle_start_day must be a day of the month, 1 to 31")
    return day


def _short_path(path: Path) -> str:
    """A path with the home directory collapsed, for printing."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _print_credentials() -> None:
    """Where each credential resolves from -- never what it is.

    Printing only the keyring names meant printing nothing at all on a machine
    that keeps its credentials in ``.env``, which is indistinguishable from a
    keyring that is not being read. Every name is listed with its source, and
    the backend is named so an empty keyring column says "nothing stored here"
    rather than "not looking".
    """
    import os

    from ..secrets import SECRET_ENV, keyring_backend
    from ..sources.homeassistant import load_dotenv

    backend = keyring_backend()
    print(f"keyring: {backend}" if backend else "keyring: none available here")

    dotenv = load_dotenv()
    stored = set(configured_secrets())
    rows = []
    for name in SECRET_NAMES:
        variable = SECRET_ENV[name]
        if os.environ.get(variable):
            rows.append((name, f"environment ({variable})"))
        elif dotenv.get(variable):
            rows.append((name, f".env ({variable})"))
        elif name in stored:
            rows.append((name, "keyring"))
        else:
            rows.append((name, "not set"))

    width = max(len(name) for name, _ in rows) + 2
    print()
    for name, source in rows:
        print(f"  {name:<{width}}{source}")
    print("\nthe environment and .env win over the keyring; values are never printed")


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
    from .account_commands import (
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
        _print_account(profile, json_output=args.json)
        return 0

    if command == "periods":
        return _run_account_periods(args, store, profile)

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
                    {"account": updated.to_dict(), "applied": args.apply},
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"updated the account at {args.effective}")
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
                print(json.dumps({"output": str(args.output)}))
        return 0

    if command == "source":
        from .account_commands import meter_source_summary, set_meter_source

        if args.source_command == "show":
            summary = meter_source_summary(profile, args.provider)
            if args.json:
                print(json.dumps(summary, indent=2))
            elif summary["configured"]:
                print(f"source: {summary['source']}")
                print(f"grid import: {summary['grid_import_entity']}")
                print(f"grid export: {summary['grid_export_entity']}")
            else:
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
            print("account meter sources")
            print(f"source: {args.provider}")
            print(f"grid import: {summary['grid_import_entity']}")
            print(f"grid export: {summary['grid_export_entity']}")
            if not args.apply:
                print("preview only; pass --apply to save")
        return 0

    raise AssertionError(f"unhandled account command {command}")


def _run_account_periods(args: Any, store: Any, profile: Any) -> int:
    """Store the boundaries PG&E billed on, so anything reading the account has them.

    The portal lists its own cycles and needs no PDF to do it, but only the CLI
    has credentials for it. Writing them onto the account is what carries them
    anywhere else the account goes -- `account export` into Home Assistant's
    config entry, most of all, which is otherwise left guessing at a
    meter-read day.
    """
    from dataclasses import replace

    from ..sources import PgeSettings, cached_bill_periods

    periods = cached_bill_periods(PgeSettings.load(config_path=args.config), refresh=True)
    if not periods:
        raise ConfigError("the portal listed no billing periods")
    updated = replace(profile, billing_periods=tuple(periods))
    if args.apply:
        updated = store.save(updated, expected_revision=profile.revision)

    if args.json:
        print(
            json.dumps(
                {
                    "billing_periods": [
                        {"start": p.start.isoformat(), "end": p.end.isoformat()} for p in periods
                    ],
                    "applied": args.apply,
                },
                indent=2,
            )
        )
        return 0

    print(f"{_plural(len(periods), 'billing period')}, {periods[0].start} to {periods[-1].end}")
    for period in periods[-3:]:
        print(f"  {period.start} to {period.end}")
    if not args.apply:
        print("preview only; pass --apply to save")
    return 0


def _mqtt_settings(args: Any, *, from_account: bool) -> Any:
    """Build MQTT settings, keeping ``--config``'s stateless choice authoritative.

    ``[mqtt].account`` in the config file can also ask to price from the
    account, but ``_pricing_context`` has already resolved that precedence for
    every command: a ``--config`` file was named, so nothing else applies. It
    is passed as an explicit override because an override is what outranks the
    file.
    """
    from ..mqtt import MqttSettings

    return MqttSettings.load(
        config_path=args.config,
        broker=args.broker,
        port=args.port,
        username=args.username,
        topic_prefix=args.topic_prefix,
        discovery=args.discovery,
        forecast_hours=args.forecast_hours,
        tls=args.tls,
        allow_insecure_auth=args.allow_insecure_auth,
        account=from_account,
    )


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
                set_secret(args.name, value)
                print(f"stored {args.name}")
            elif args.credential_command == "delete":
                delete_secret(args.name)
                print(f"deleted {args.name}")
            else:
                _print_credentials()
            return 0

        if args.command == "account":
            return _run_account_command(args)

        engine, config, account_profile = _pricing_context(args)
        from_account = account_profile is not None

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
            from ..billing import BillEngine, BillingPeriod
            from ..sources import read_green_button

            # Resolved before any source is read, because every one of them is
            # asked for a window and a CSV on stdin is the only case where the
            # readings themselves can supply it.
            # A CSV names its own window when no dates are given -- but only
            # for the source that reads a CSV. Every other source is asked for
            # a period, so it has to be resolved even when a path was passed.
            reads_csv = args.source in {"green-button", "csv"} and args.csv is not None
            period, cycle_basis = (
                (None, "")
                if reads_csv and not (args.start or args.end)
                else _billing_window(args, account_profile)
            )
            note = ""
            if args.source == "ha":
                from ..sources import HaSettings, describe_resolution, read_statistics

                assert period is not None  # every source but the CSV resolves one
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
                    _midnight(period.start),
                    _midnight(period.end) + timedelta(days=1),
                    resolution=args.ha_resolution,
                )
                note = f"  source: Home Assistant statistics ({describe_resolution(readings)})"
            elif args.source == "influx":
                from ..sources import InfluxSettings, describe_resolution, read_counters

                assert period is not None
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
                    _midnight(period.start),
                    _midnight(period.end) + timedelta(days=1),
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
            elif args.csv is not None:
                readings = read_green_button(sys.stdin if str(args.csv) == "-" else args.csv)
                note = f"  source: Green Button CSV ({len(readings)} intervals)"
            else:
                from ..sources import PgeSettings, cached_green_button

                assert period is not None
                export = cached_green_button(
                    PgeSettings.load(config_path=args.config),
                    period.start,
                    period.end,
                    refresh=args.refresh,
                )
                if cycle_basis and export.end < period.end:
                    # The utility publishes a day behind, so an open cycle asked
                    # for "through today" ends at the last read instead. Pricing
                    # days it has no readings for would charge the Base Services
                    # Charge for each and call the shortfall a gap in the meter.
                    period = BillingPeriod(period.start, export.end)
                readings = read_green_button(export.path)
                origin = "downloaded" if export.downloaded else f"cached {export.covers}"
                note = (
                    f"  source: Green Button, {origin} "
                    f"({len(readings)} intervals, {_short_path(export.path)})"
                )

            if period is None:
                period = BillingPeriod.from_readings(readings)
            if from_account:
                from ..billing.engine import compute_segments

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
                if cycle_basis:
                    print(f"  cycle: {period.start} to {period.end}, {_BASIS[cycle_basis]}")
            return 0

        if args.command == "mqtt":
            from ..mqtt import MqttPublisher

            settings = _mqtt_settings(args, from_account=from_account)
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

            from ..web import create_app

            uvicorn.run(
                create_app(
                    config,
                    profile=account_profile,
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
