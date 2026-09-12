"""Turning this front end's flags into a meter reader.

The only place in the CLI that knows a source can be more than one thing.
`bill` opens a reader and asks it for readings; which reader it got, and what
had to be configured for that reader to exist, is settled here.

Deliberately not in the library. Which source a *user of this CLI* should get
by default is an answer about this front end -- the Home Assistant integration
faces the same question and answers it with a config entry and an entity
picker, not with `--source` and environment variables.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

from ..errors import ConfigError, TariffKitError

if TYPE_CHECKING:
    from ..sources.meters import MeterReader


def _default_meter_source(args: argparse.Namespace, profile: object | None) -> str:
    """Which source to read when nobody said, preferring one that is set up.

    This used to be the constant "ha", which meant an account that priced its
    bills from InfluxDB, or from a Green Button export it had already
    downloaded, was told that HA_TOKEN was not set -- naming the one source it
    had not configured rather than any of the ones it had.
    """
    from .availability import choose_meter_source

    if args.csv is not None:
        return "green-button"
    # Naming *either* of a source's entities on the command line is asking for
    # that source. The survey only sees the config file and the profile, so
    # without this a `--ha-import-entity` run reported Home Assistant
    # unconfigured and quietly priced from somewhere else -- or refused, while
    # telling the user to name the very counters they had just named.
    #
    # Either, not both: `HaSettings.load` merges an override with the other
    # entity from the profile or the config file, so one flag can complete a
    # half-configured source. Requiring both ignored that and switched source
    # instead. If the merged pair is still incomplete, `require_entities` says
    # which half is missing, which is a better answer than picking a different
    # source.
    if args.ha_import_entity or args.ha_export_entity:
        return "ha"
    if args.influx_import_entity or args.influx_export_entity:
        return "influx"
    chosen, looked = choose_meter_source(args.config, profile)
    if chosen is not None:
        return chosen
    remedies = "\n".join(
        f"  {status.name:<20}{status.remedy}" for status in looked if not status.available
    )
    raise ConfigError(
        "no meter source is configured, so there are no readings to price a "
        "cycle from. Configure one of:\n"
        f"{remedies}\n"
        "or price an export you already have with `tariffkit bill --csv <file>`. "
        "Rates themselves need none of this: `tariffkit now`, `forecast` and "
        "`info` work as they are."
    )


def open_meter(args: argparse.Namespace, profile: object | None) -> MeterReader:
    """Turn the command line into a reader, choosing one if nobody said.

    The only place in the CLI that knows a source can be more than one thing.
    Everything downstream asks the reader for readings and prints what the
    reader calls itself.
    """
    from ..sources import HaSettings, InfluxSettings, PgeSettings
    from ..sources.meters import (
        GreenButtonExport,
        GreenButtonFile,
        HomeAssistantMeter,
        InfluxMeter,
    )

    sources = getattr(profile, "meter_sources", None)
    chosen = args.source or _default_meter_source(args, profile)
    if chosen in {"green-button", "csv"} and args.csv is not None:
        return GreenButtonFile(sys.stdin if str(args.csv) == "-" else args.csv)
    if chosen == "ha":
        return HomeAssistantMeter(
            HaSettings.load(
                config_path=args.config,
                profile_source=getattr(sources, "ha", None),
                import_entity=args.ha_import_entity,
                export_entity=args.ha_export_entity,
            ),
            resolution=args.ha_resolution,
        )
    if chosen == "influx":
        return InfluxMeter(
            InfluxSettings.load(
                config_path=args.config,
                profile_source=getattr(sources, "influx", None),
                import_entity=args.influx_import_entity,
                export_entity=args.influx_export_entity,
            ),
            minutes=args.influx_resolution,
        )
    try:
        pge: Any = PgeSettings.load(config_path=args.config)
    except TariffKitError:
        # No login is not fatal: a cached export still prices, and the reader
        # says what to do when none covers the window.
        pge = None
    return GreenButtonExport(pge, refresh=args.refresh)
