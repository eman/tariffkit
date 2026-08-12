"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .engine import RateEngine
from .errors import ConfigError, NemRatesError
from .models import PriceCurve, PricePoint
from .timeutil import PACIFIC, to_pacific


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nem-rates",
        description="PG&E E-ELEC import/export prices under NEM 3.0.",
    )
    parser.add_argument("--version", action="version", version=f"nem-rates {__version__}")
    parser.add_argument("--config", type=Path, help="path to a config TOML file")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    now = sub.add_parser("now", help="current import and export price")
    now.add_argument("--json", action="store_true", help="emit JSON")

    forecast = sub.add_parser("forecast", help="upcoming hourly prices")
    forecast.add_argument("--hours", type=int, default=24)
    forecast.add_argument("--start", type=datetime.fromisoformat, help="ISO 8601 with offset")
    forecast.add_argument("--format", choices=("table", "json", "csv"), default="table")

    sub.add_parser("info", help="which data is loaded, and from where")

    bill = sub.add_parser("bill", help="compute a bill from interval meter data")
    bill.add_argument(
        "csv", type=Path, nargs="?", help="interval data CSV; '-' for stdin. Omit with --source ha"
    )
    bill.add_argument(
        "--source",
        choices=("csv", "ha"),
        default="csv",
        help="where the readings come from (default: csv)",
    )
    bill.add_argument("--start", type=date.fromisoformat, help="cycle start (meter read date)")
    bill.add_argument("--end", type=date.fromisoformat, help="cycle end, inclusive")
    bill.add_argument("--json", action="store_true")
    bill.add_argument("--no-check", dest="check", action="store_false", help="skip coverage checks")
    bill.add_argument("--ha-import-entity", help="override the grid-import entity")
    bill.add_argument("--ha-export-entity", help="override the grid-export entity")
    bill.add_argument(
        "--ha-resolution",
        choices=("auto", "5minute", "hour"),
        default="auto",
        help="statistics resolution; auto prefers 5-minute where it still exists",
    )

    mqtt = sub.add_parser("mqtt", help="publish to MQTT every hour")
    mqtt.add_argument("--broker", required=True)
    mqtt.add_argument("--port", type=int, default=1883)
    mqtt.add_argument("--username")
    mqtt.add_argument("--password")
    mqtt.add_argument("--topic-prefix", default="nem_rates")
    mqtt.add_argument("--forecast-hours", type=int, default=48)
    mqtt.add_argument("--tls", action="store_true")
    mqtt.add_argument(
        "--no-discovery",
        dest="discovery",
        action="store_false",
        help="skip Home Assistant discovery config",
    )
    mqtt.add_argument("--once", action="store_true", help="publish once and exit")

    serve = sub.add_parser("serve", help="run the REST API")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = Config.load(args.config)
        engine = RateEngine(config)

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
            info["exact_through"] = engine.export_rates.exact_through
            info["daily_fixed_charge"] = engine.daily_fixed_charge()
            print(json.dumps(info, indent=2, default=str))
            return 0

        if args.command == "bill":
            from .billing import BillEngine, BillingPeriod, read_csv

            note = ""
            if args.source == "ha":
                from .sources import HaSettings, describe_resolution, read_statistics

                if not (args.start and args.end):
                    raise ConfigError("--source ha requires --start and --end")
                ha_settings = HaSettings.load(
                    config_path=args.config,
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
            elif args.csv is None:
                raise ConfigError("give a CSV path, or use --source ha")
            else:
                readings = read_csv(sys.stdin if str(args.csv) == "-" else args.csv)

            period = (
                BillingPeriod(args.start, args.end)
                if args.start and args.end
                else BillingPeriod.from_readings(readings)
            )
            result = BillEngine(engine).compute(readings, period, check=args.check)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                _print_bill(result)
                if note:
                    print(note)
            return 0

        if args.command == "mqtt":
            from .mqtt import MqttPublisher, MqttSettings

            settings = MqttSettings(
                broker=args.broker,
                port=args.port,
                username=args.username,
                password=args.password,
                topic_prefix=args.topic_prefix,
                discovery=args.discovery,
                forecast_hours=args.forecast_hours,
                tls=args.tls,
            )
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

            uvicorn.run(create_app(config), host=args.host, port=args.port)
            return 0

    except NemRatesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130

    # Unreachable: argparse enforces `required=True` on the subparser, so an
    # unknown command exits before we get here.
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
