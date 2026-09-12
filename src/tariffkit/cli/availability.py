"""What each data source can answer right now, and what it cannot.

This is CLI code, not library code: the remedies name `tariffkit` commands and
the environment variables this front end documents, which is knowledge about
*this* client rather than about tariffs. A different client -- the Home
Assistant integration -- answers the same question in its own vocabulary, with
an entity picker and a config entry.

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
    from ..sources.homeassistant import HaSettings

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
    from ..sources.influx import InfluxSettings

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
    from ..sources.pge import PgeSettings

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


def _short_path(path: Path) -> str:
    """A path with the home directory collapsed.

    These lines get pasted into bug reports, and an absolute path under a home
    directory carries the account name with it.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _account() -> SourceStatus:
    """The account itself is evidence, and most commands need it.

    Listing only the meters left the one prerequisite off the page: without an
    account there is no tariff history, no statement evidence, and nothing that
    knows when the current cycle began -- so `bill` needs explicit dates and
    `account periods` has nothing to record against.
    """
    from .account_store import AccountStore

    features = (
        "bill without --start/--end",
        "cycle boundaries from statements",
        "dated tariff history",
    )
    try:
        store = AccountStore()
        if not store.exists():
            raise FileNotFoundError
        store.load()
    except Exception:
        return SourceStatus(
            "account",
            False,
            features,
            "run `tariffkit account init --tariff ... --supplier ... --pto-date ...`",
        )
    return SourceStatus("account", True, features)


def _green_button_cache() -> SourceStatus:
    from ..sources.pge import green_button_cache_dir

    features = ("bill --source green-button, without a login",)
    base = green_button_cache_dir()
    held = sorted(base.glob("*.csv")) if base.is_dir() else []
    if not held:
        return SourceStatus(
            "green_button_cache",
            False,
            features,
            f"nothing cached under {_short_path(base)}; a download with credentials "
            "fills it, or pass an export with `bill --csv`",
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
        _account(),
        _ha(config_path, profile),
        _influx(config_path, profile),
        _pge(config_path),
        _green_button_cache(),
    )


#: Which status answers for each `--source`, in preference order. The cache is
#: last because it only answers for windows it already holds.
_SOURCE_STATUS = (
    ("ha", "home_assistant"),
    ("influx", "influxdb"),
    ("green-button", "pge_portal"),
    ("green-button", "green_button_cache"),
)


def first_available_meter_source(
    statuses: tuple[SourceStatus, ...],
) -> str | None:
    """The `--source` to use when the caller did not name one, or ``None``."""
    by_name = {status.name: status for status in statuses}
    for source, key in _SOURCE_STATUS:
        if by_name[key].available:
            return source
    return None


def choose_meter_source(
    config_path: str | Path | None = None, profile: object | None = None
) -> tuple[str | None, tuple[SourceStatus, ...]]:
    """The first available source, probing only as far as it has to.

    `survey` asks every source, and asking costs a keyring lookup each: on a
    macOS Keychain that is an access record, and potentially a prompt, for a
    utility password on a machine that only ever reads Home Assistant. Choosing
    needs the *first* answer, so it stops there and reports only what it looked
    at. The full survey is for `tariffkit sources`, where the point is the whole
    picture.
    """
    probes = {
        "home_assistant": lambda: _ha(config_path, profile),
        "influxdb": lambda: _influx(config_path, profile),
        "pge_portal": lambda: _pge(config_path),
        "green_button_cache": _green_button_cache,
    }
    looked: list[SourceStatus] = []
    for source, key in _SOURCE_STATUS:
        status = next((s for s in looked if s.name == key), None)
        if status is None:
            status = probes[key]()
            looked.append(status)
        if status.available:
            return source, tuple(looked)
    return None, tuple(looked)
