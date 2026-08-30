"""Interval readings from Home Assistant's long-term statistics.

The meter reader -- a Rainforest Eagle-100 on the smart meter -- publishes
cumulative kWh counters for grid import and export. Home Assistant records those
as long-term statistics, which is the only place a full billing cycle survives:
the states history behind ``/api/history`` is purged on the recorder's schedule,
typically ten days, while statistics are kept indefinitely.

Statistics are only reachable over the WebSocket API, so this module needs
``websockets`` and lives outside :mod:`tariffkit.billing`, which is deliberately
stdlib-only. It produces ``IntervalReading`` objects that the billing engine
consumes exactly as it consumes a CSV.

Two resolutions exist and they are kept for different lengths of time:

* ``5minute`` -- roughly the recorder's retention window, so recent cycles only.
* ``hour`` -- kept indefinitely.

The default asks for both and prefers the finer one wherever it exists. That
matters beyond tidiness: import and export are metered separately, so a slot
carrying both directions is real rather than un-netted gross data, and the
coarser the slot the more often that happens. On one real week, 42% of active
hours carried both directions against 12% of five-minute slots.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..account.model import MeterSource
from ..billing.models import IntervalReading
from ..config import default_config_path
from ..errors import ConfigError, DataError
from ..secrets import get_secret
from ..timeutil import to_pacific

log = logging.getLogger(__name__)

Resolution = Literal["auto", "5minute", "hour"]

#: Home Assistant's own period names, finest first.
PERIODS: dict[str, timedelta] = {"5minute": timedelta(minutes=5), "hour": timedelta(hours=1)}

#: The Rainforest Eagle-100 pair, monotonic-filtered. The unfiltered entities are
#: named ``..._total_energy_delivered`` and drop to zero several times a day when
#: the device re-establishes its meter session, so they are the wrong default
#: despite the more official-looking name.
DEFAULT_IMPORT_ENTITY = "sensor.eagle_100_energy_delivered"
DEFAULT_EXPORT_ENTITY = "sensor.eagle_100_energy_received"

#: Ceiling on implied power for one interval, in kW. Anything above it is a
#: counter artefact rather than energy.
#:
#: Statistics restart their running ``sum`` when recording is interrupted, and
#: the first point of the new epoch reports the whole accumulated total as its
#: ``change``. One real instance put 543.663 kWh in a five-minute slot -- about
#: 6,500 kW, against a 200 A service that tops out near 48 kW. Set well above any
#: residential service so it only ever catches the impossible.
MAX_INTERVAL_KW = 100.0


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Parse a ``.env`` file leniently, returning what it defines.

    Tolerates ``KEY = "value"`` with spaces around the equals and quotes around
    the value, which is how these files are usually written by hand. Missing
    files yield nothing rather than raising: a token may equally come from the
    environment.
    """
    found: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return found
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


@dataclass(frozen=True, slots=True)
class HaSettings:
    """Where to reach Home Assistant, and which entities carry grid exchange."""

    host: str
    #: Never printed. `repr=False` keeps it out of tracebacks, which render
    #: dataclass frames -- the same reason PgeSettings marks its own.
    token: str = field(repr=False)
    import_entity: str = DEFAULT_IMPORT_ENTITY
    export_entity: str = DEFAULT_EXPORT_ENTITY

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.host.startswith("https") else "ws"
        base = self.host.split("://", 1)[-1].rstrip("/")
        return f"{scheme}://{base}/api/websocket"

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        dotenv_path: str | Path = ".env",
        profile_source: MeterSource | None = None,
        **overrides: str | None,
    ) -> HaSettings:
        """Resolve settings from config file, ``.env``, environment, and args.

        Later wins: ``[home_assistant]`` in the config file, then ``.env``, then
        real environment variables, then a named profile's grid-import/grid-
        export mapping, then explicit overrides.

        The token is deliberately not read from the config file. Entity ids are
        configuration and belong somewhere shareable; a long-lived access token
        is not, and the config file is not gitignored.
        """
        values: dict[str, str] = {}
        path = Path(config_path) if config_path else default_config_path()
        if path.is_file():
            table = tomllib.loads(path.read_text(encoding="utf-8")).get("home_assistant", {})
            for key in ("host", "import_entity", "export_entity"):
                if key in table:
                    values[key] = str(table[key])

        env = {**load_dotenv(dotenv_path), **os.environ}
        if host := env.get("HA_HOST"):
            values["host"] = host
        if token := env.get("HA_TOKEN") or get_secret("home_assistant.token"):
            values["token"] = token
        for key, name in (
            ("import_entity", "TARIFFKIT_HA_IMPORT_ENTITY"),
            ("export_entity", "TARIFFKIT_HA_EXPORT_ENTITY"),
        ):
            if value := env.get(name):
                values[key] = value

        if profile_source is not None:
            if not isinstance(profile_source, MeterSource):
                raise ConfigError("profile_source must be a MeterSource")
            values["import_entity"] = profile_source.grid_import_entity
            values["export_entity"] = profile_source.grid_export_entity

        values.update({k: v for k, v in overrides.items() if v})

        missing = [k for k in ("host", "token") if not values.get(k)]
        if missing:
            raise ConfigError(
                f"Home Assistant {' and '.join(missing)} not set; put HA_HOST and "
                f"HA_TOKEN in the environment, or store home_assistant.token with "
                f"`tariffkit credentials set`"
            )
        return cls(
            host=values["host"],
            token=values["token"],
            import_entity=values.get("import_entity", DEFAULT_IMPORT_ENTITY),
            export_entity=values.get("export_entity", DEFAULT_EXPORT_ENTITY),
        )


def _readings_from(
    series: dict[str, list[dict[str, Any]]],
    settings: HaSettings,
    duration: timedelta,
    max_kw: float = MAX_INTERVAL_KW,
) -> dict[int, IntervalReading]:
    """Turn one resolution's statistics into readings keyed by epoch-ms start.

    Uses each point's ``change`` -- the energy within that period -- rather than
    differencing ``sum`` between points. Both are available, but ``change`` is
    already aligned to its own period, so it needs no off-by-one correction and
    yields a value for the first point instead of discarding it.
    """
    imported = {p["start"]: p.get("change") or 0.0 for p in series.get(settings.import_entity, [])}
    exported = {p["start"]: p.get("change") or 0.0 for p in series.get(settings.export_entity, [])}

    ceiling = max_kw * duration.total_seconds() / 3600
    readings: dict[int, IntervalReading] = {}
    for stamp in sorted(set(imported) | set(exported)):
        start = to_pacific(datetime.fromtimestamp(stamp / 1000, tz=UTC))
        # A counter that goes backwards is a device or restart artefact, not
        # energy flowing the other way; clamp rather than raise on it.
        into = max(imported.get(stamp, 0.0), 0.0)
        out = max(exported.get(stamp, 0.0), 0.0)
        if into > ceiling or out > ceiling:
            # Dropped rather than clamped: the reading is not merely large, it is
            # not a reading at all, and leaving a hole lets coverage checking
            # report it instead of quietly inventing a plausible number.
            log.warning(
                "discarding %s statistics point: %.3f kWh in / %.3f kWh out over %s "
                "implies more than %.0f kW, so the running sum restarted here",
                start.isoformat(),
                into,
                out,
                duration,
                max_kw,
            )
            continue
        readings[stamp] = IntervalReading(
            start=start, imported=into, exported=out, duration=duration
        )
    return readings


async def _fetch(
    settings: HaSettings, start: datetime, end: datetime, periods: list[str]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - exercised by packaging
        raise RuntimeError(
            "the Home Assistant source requires the 'ha' extra: pip install 'tariffkit[ha]'"
        ) from exc

    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    async with websockets.connect(settings.websocket_url, max_size=None, open_timeout=30) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": settings.token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise ConfigError("Home Assistant rejected HA_TOKEN")
        for index, period in enumerate(periods, start=1):
            await ws.send(
                json.dumps(
                    {
                        "id": index,
                        "type": "recorder/statistics_during_period",
                        "start_time": start.astimezone(UTC).isoformat(),
                        "end_time": end.astimezone(UTC).isoformat(),
                        "statistic_ids": [settings.import_entity, settings.export_entity],
                        "period": period,
                        "types": ["change"],
                    }
                )
            )
            # Match on id rather than taking the next frame: Home Assistant may
            # interleave other messages, and pairing the wrong payload with a
            # period would silently bill one resolution's data as another's.
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == index:
                    break
            if not message.get("success"):
                error = (message.get("error") or {}).get("message", "unknown error")
                raise DataError(f"Home Assistant refused the {period} statistics request: {error}")
            out[period] = message.get("result") or {}
    return out


async def read_statistics_async(
    settings: HaSettings,
    start: datetime,
    end: datetime,
    resolution: Resolution = "auto",
    max_kw: float = MAX_INTERVAL_KW,
) -> list[IntervalReading]:
    """Interval readings for ``[start, end)`` from Home Assistant statistics.

    ``auto`` asks for both resolutions and prefers five-minute wherever it
    exists, falling back to hourly for the rest of the window. One run can
    therefore mix the two; :func:`describe_resolution` reports what was used, so
    a caller can say so rather than implying uniformity.
    """
    if resolution not in ("auto", *PERIODS):
        raise ConfigError(f"unknown resolution {resolution!r}; use auto, 5minute or hour")
    for name, moment in (("start", start), ("end", end)):
        if moment.tzinfo is None:
            # Naive datetimes do not raise here, they silently assume the
            # machine's timezone -- so the window would be quietly wrong rather
            # than obviously broken.
            raise ConfigError(f"{name} must be timezone-aware; got {moment.isoformat()}")
    if end <= start:
        raise ConfigError(f"end {end.isoformat()} is not after start {start.isoformat()}")

    periods: list[str] = ["5minute", "hour"] if resolution == "auto" else [resolution]
    fetched = await _fetch(settings, start, end, periods)

    hourly = _readings_from(fetched.get("hour", {}), settings, PERIODS["hour"], max_kw)
    fine = _readings_from(fetched.get("5minute", {}), settings, PERIODS["5minute"], max_kw)

    # Drop any hour the fine series already accounts for, or the same energy is
    # counted twice. Tested per hour rather than over the fine series' overall
    # span, so a hole in it falls back to hourly instead of vanishing.
    hour_ms = int(PERIODS["hour"].total_seconds() * 1000)
    fine_hours = {stamp - stamp % hour_ms for stamp in fine}
    readings = {stamp: r for stamp, r in hourly.items() if stamp not in fine_hours}
    readings.update(fine)

    if not readings:
        raise DataError(
            f"no statistics for {settings.import_entity} / {settings.export_entity} "
            f"between {start.isoformat()} and {end.isoformat()}"
        )
    return [readings[stamp] for stamp in sorted(readings)]


def read_statistics(
    settings: HaSettings,
    start: datetime,
    end: datetime,
    resolution: Resolution = "auto",
    max_kw: float = MAX_INTERVAL_KW,
) -> list[IntervalReading]:
    """Blocking wrapper around :func:`read_statistics_async`.

    Refuses to run inside an existing event loop rather than letting
    ``asyncio.run`` raise a bare RuntimeError, which says nothing about what to
    do instead. Async callers -- the REST app among them -- want the coroutine.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ConfigError(
            "read_statistics() blocks and cannot run inside an event loop; "
            "await read_statistics_async() instead"
        )
    return asyncio.run(read_statistics_async(settings, start, end, resolution, max_kw))


def describe_resolution(readings: list[IntervalReading]) -> str:
    """One line naming the resolutions present, for the caller to report."""
    counts: dict[timedelta, int] = {}
    for r in readings:
        counts[r.duration] = counts.get(r.duration, 0) + 1
    names = {v: k for k, v in PERIODS.items()}
    parts = [
        f"{count} x {names.get(duration, str(duration))}"
        for duration, count in sorted(counts.items())
    ]
    return ", ".join(parts)


def settings_with(settings: HaSettings, **overrides: str | None) -> HaSettings:
    """Apply non-empty overrides to ``settings``."""
    return replace(settings, **{k: v for k, v in overrides.items() if v})
