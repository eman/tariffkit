"""What each data source can answer right now, and what it cannot.

Every source here is optional, and the useful combinations are not a line: an
account with no utility login still prices a cycle from Home Assistant, one
with no meter integration still prices a downloaded interval export, and one
with neither still answers what a kilowatt-hour costs this hour. Rate pricing
is the only thing that always works, because the rate data ships in the wheel.

The point of surveying rather than discovering is the error message. Asking a
source that was never configured used to fail with that source's own complaint
-- "HA_TOKEN not set" -- which tells a reader what is missing but not that
three other answers were available. A caller that knows what is configured can
pick one, and say what it picked.

Availability is about *configuration*, not reachability: a host that is down
still counts as configured, because the alternative is a network round trip in
front of every command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import TariffKitError

#: Sources that can supply interval readings for a billing period, best first.
#: Order is preference when nobody asked for one in particular: a local meter
#: integration is faster and reaches further back than a portal download, and
#: the cache is only worth using when it already holds the window.
METER_SOURCES = ("ha", "influx", "green-button")


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Whether one source is configured, and what it is good for."""

    name: str
    available: bool
    #: What it answers. Printed beside the name so a reader can tell what
    #: turning it on would buy, not just that it is off.
    features: tuple[str, ...]
    #: Empty when available; otherwise what to do about it.
    remedy: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "features": list(self.features),
            "remedy": self.remedy,
        }


def _ha(config_path: str | Path | None, profile: object | None) -> SourceStatus:
    from .homeassistant import HaSettings

    features = ("bill --source ha",)
    try:
        settings = HaSettings.load(
            config_path=config_path,
            profile_source=getattr(getattr(profile, "meter_sources", None), "home_assistant", None),
        )
    except TariffKitError:
        return SourceStatus(
            "home_assistant",
            False,
            features,
            "set HA_HOST and HA_TOKEN, or store home_assistant.token with "
            "`tariffkit credentials set`",
        )
    if not (settings.import_entity and settings.export_entity):
        return SourceStatus(
            "home_assistant",
            False,
            features,
            "name the grid counters with `tariffkit account source set ha "
            "--grid-import-entity ... --grid-export-entity ...`",
        )
    return SourceStatus("home_assistant", True, features)


def _influx(config_path: str | Path | None, profile: object | None) -> SourceStatus:
    from .influx import InfluxSettings

    features = ("bill --source influx",)
    try:
        settings = InfluxSettings.load(
            config_path,
            profile_source=getattr(getattr(profile, "meter_sources", None), "influx", None),
        )
    except TariffKitError:
        return SourceStatus(
            "influxdb",
            False,
            features,
            "set INFLUXDB3_HOST, INFLUXDB3_DATABASE and INFLUXDB3_AUTH_TOKEN",
        )
    if not (settings.import_entity and settings.export_entity):
        return SourceStatus(
            "influxdb",
            False,
            features,
            "name the grid counters with `tariffkit account source set influx "
            "--grid-import-entity ... --grid-export-entity ...`",
        )
    return SourceStatus("influxdb", True, features)


def _pge(config_path: str | Path | None) -> SourceStatus:
    from .pge import PgeSettings

    features = (
        "bill --source green-button",
        "statement download",
        "billing period boundaries",
    )
    try:
        PgeSettings.load(config_path=config_path)
    except TariffKitError:
        return SourceStatus(
            "pge_portal",
            False,
            features,
            "store pge.username and pge.password with `tariffkit credentials set`, "
            "or set PGE_USERNAME and PGE_PASSWORD",
        )
    return SourceStatus("pge_portal", True, features)


def _green_button_cache() -> SourceStatus:
    from .pge import _default_export_cache

    features = ("bill --source green-button, without a login",)
    base = _default_export_cache()
    held = sorted(base.glob("*.csv")) if base.is_dir() else []
    if not held:
        return SourceStatus(
            "green_button_cache",
            False,
            features,
            f"nothing cached under {base}; a download with credentials fills it, "
            "or pass an export with `bill --csv`",
        )
    return SourceStatus("green_button_cache", True, features)


def survey(
    config_path: str | Path | None = None, profile: object | None = None
) -> tuple[SourceStatus, ...]:
    """Every source, whether it is configured, and what to do if it is not.

    Rates come first and are always available: they ship in the wheel, so the
    answer to "what does a kilowatt-hour cost right now" never depends on any
    of the rest.
    """
    return (
        SourceStatus("rates", True, ("now", "forecast", "info", "serve", "mqtt")),
        _ha(config_path, profile),
        _influx(config_path, profile),
        _pge(config_path),
        _green_button_cache(),
    )


def first_available_meter_source(
    statuses: tuple[SourceStatus, ...],
) -> str | None:
    """The `--source` to use when the caller did not name one, or ``None``."""
    by_name = {status.name: status for status in statuses}
    for source in METER_SOURCES:
        key = {
            "ha": "home_assistant",
            "influx": "influxdb",
            "green-button": "pge_portal",
        }[source]
        if by_name[key].available:
            return source
    if by_name["green_button_cache"].available:
        return "green-button"
    return None
