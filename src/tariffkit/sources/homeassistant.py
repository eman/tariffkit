"""Interval readings from Home Assistant's long-term statistics.

The meter reader -- a device paired with the smart meter -- publishes cumulative
kWh counters for grid import and export. Home Assistant records those
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

# `load_dotenv` is re-exported: it lives in `secrets` now, but callers have
# always imported it from here.
from ..metering import MAX_INTERVAL_KW, carry, interval_energy
from ..secrets import get_secret, load_dotenv
from ..timeutil import to_pacific

log = logging.getLogger(__name__)

Resolution = Literal["auto", "5minute", "hour"]

#: Home Assistant's own period names, finest first.
PERIODS: dict[str, timedelta] = {"5minute": timedelta(minutes=5), "hour": timedelta(hours=1)}

#: Nothing is assumed about which entities carry grid exchange. There used to be
#: a default pair here, named for the hardware this was developed against, which
#: meant an unconfigured install quietly asked Home Assistant about somebody
#: else's sensors. Entity names are site-specific; a wrong guess is not a
#: better starting point than no guess.


@dataclass(frozen=True, slots=True)
class HaSettings:
    """Where to reach Home Assistant, and which entities carry grid exchange."""

    host: str
    #: Never printed. `repr=False` keeps it out of tracebacks, which render
    #: dataclass frames -- the same reason PgeSettings marks its own.
    token: str = field(repr=False)
    #: Optional: rate pricing needs neither. ``None`` means "not configured",
    #: which is answered when a read is attempted, not at load time.
    import_entity: str | None = None
    export_entity: str | None = None

    def require_entities(self) -> tuple[str, str]:
        """The two entity ids, or a ConfigError naming how to set them."""
        missing = [
            name
            for name, value in (
                ("import_entity", self.import_entity),
                ("export_entity", self.export_entity),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Home Assistant {' and '.join(missing)} not set; name the grid "
                f"counters with `tariffkit account source set ha "
                f"--grid-import-entity ... --grid-export-entity ...`, or put "
                f"import_entity/export_entity under [home_assistant] in the "
                f"config file. Rate pricing (`tariffkit now`, `forecast`, "
                f"`info`) needs neither."
            )
        assert self.import_entity is not None and self.export_entity is not None
        return self.import_entity, self.export_entity

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.host.startswith("https") else "ws"
        base = self.host.split("://", 1)[-1].rstrip("/")
        return f"{scheme}://{base}/api/websocket"

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        dotenv_path: str | Path | None = None,
        profile_source: MeterSource | None = None,
        **overrides: str | None,
    ) -> HaSettings:
        """Resolve settings from config file, ``.env``, environment, and args.

        Later wins: ``[home_assistant]`` in the config file, then ``.env``, then
        real environment variables, then the account's grid-import/grid-export
        mapping, then explicit overrides.

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
            import_entity=values.get("import_entity") or None,
            export_entity=values.get("export_entity") or None,
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

    **Each direction is judged on its own.** Import and export are separate
    entities that restart their running ``sum`` independently, and refusing the
    whole interval when either one did let a single bad series destroy the
    other's good energy. Measured against an unfiltered smart-meter export
    counter, whose session resets 5.5 times a day: the export series' 56 bad
    hours took 21.4 kWh of perfectly good *import* with them, 74.5 kWh billed as
    53.1. The two entities are the reason `check_coverage` sees one series --
    they are merged into one ``IntervalReading`` here -- but nothing about a
    restart on one meter says anything about the other.

    A refused direction reads as ``0.0``, because a float cannot say "unknown" --
    so the interval records which directions it could not measure in
    ``IntervalReading.unmetered``, and :func:`check_coverage` reports them. That
    keeps the good half's energy *and* the hole, where dropping the interval
    kept only the hole and clamping to the ceiling would have kept neither.
    Only when *both* directions are refused is the interval dropped entirely:
    nothing is left to preserve, and a row of two zeros would read as a measured
    hour of no energy.
    """
    step = duration.total_seconds()

    def energies(entity: str) -> dict[int, float | None]:
        """Each interval's energy for one entity, repaired where it can be."""
        out: dict[int, float | None] = {}
        previous: tuple[float, float] | None = None
        for row in series.get(entity, []):
            slot = float(row["start"]) / 1000
            state = row.get("state")
            out[row["start"]] = interval_energy(
                row.get("change"), state, previous, slot, step, max_kw
            )
            previous = carry(previous, slot, state)
        return out

    import_entity, export_entity = settings.require_entities()
    imported = energies(import_entity)
    exported = energies(export_entity)

    readings: dict[int, IntervalReading] = {}
    for stamp in sorted(set(imported) | set(exported)):
        start = to_pacific(datetime.fromtimestamp(stamp / 1000, tz=UTC))
        # None means neither the recorder's figure nor the counter could be
        # trusted for that direction. A negative one is a device or restart
        # artefact rather than energy flowing the other way; clamp it.
        into_raw, out_raw = imported.get(stamp), exported.get(stamp)
        into = max(into_raw, 0.0) if into_raw is not None else 0.0
        out = max(out_raw, 0.0) if out_raw is not None else 0.0
        # A row that is absent says as little as one that was refused. The
        # recorder compiles an hour for any entity with a state, and a flat
        # counter still yields a change of zero -- so no row at all means the
        # entity had no state, not that nothing crossed the meter. Reading it
        # as a measured zero is the claim this branch exists to stop making in
        # the other direction, and it is the same claim.
        refused = frozenset(
            name
            for name, seen, value in (
                ("imported", stamp in imported, into_raw),
                ("exported", stamp in exported, out_raw),
            )
            if not seen or value is None
        )
        if any(value is None for value in (into_raw, out_raw)):
            log.warning(
                "discarding the %s half of the %s statistics point: the recorder's "
                "figure implies more than %.0f kW over %s and the counter could not "
                "be differenced either",
                " and ".join(sorted(refused)),
                start.isoformat(),
                max_kw,
                duration,
            )
        if len(refused) == 2:
            continue
        readings[stamp] = IntervalReading(
            start=start,
            imported=into,
            exported=out,
            duration=duration,
            unmetered=refused,
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
                        "statistic_ids": list(settings.require_entities()),
                        "period": period,
                        # `state` is the counter itself, and `interval_energy`
                        # needs it: a recorder that mistook a dropped-to-zero
                        # reading for a counter reset reports a ruined `change`
                        # and an intact `state`.
                        "types": ["change", "state"],
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
    settings.require_entities()
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

    # Drop an hour the fine series accounts for *in full*, or the same energy is
    # counted twice. Completeness is the whole test: an hour the fine series
    # only partly covers used to lose its hourly row too, and the uncovered part
    # of it simply vanished. On a real cycle the five-minute series resumed at
    # 04:20 after a restart, which deleted the 04:00 hourly row and left
    # 04:00-04:20 in neither series -- reported as a gap, and short by whatever
    # crossed the meter in those twenty minutes.
    #
    # An hour that is not fully covered therefore keeps its hourly row and gives
    # up its partial fine rows, which is the trade the coarser figure wins: the
    # hour's energy is right either way, and only its shape within the hour is
    # lost.
    hour_ms = int(PERIODS["hour"].total_seconds() * 1000)
    covered: dict[int, float] = {}
    for stamp, reading in fine.items():
        hour = stamp - stamp % hour_ms
        covered[hour] = covered.get(hour, 0.0) + reading.duration.total_seconds()
    whole = {
        hour for hour, seconds in covered.items() if seconds >= PERIODS["hour"].total_seconds() - 1
    }
    # A partial hour gives its fine rows up *to the hourly row that replaces
    # them* -- and only if there is one. Surrendering them unconditionally threw
    # away every reading in a window that begins mid-hour, where no hourly row
    # can exist: an explicit five-minute request for 04:20-05:00 has eight rows
    # per entity and returned "no statistics" for the period.
    surrendered = {hour for hour in covered if hour not in whole and hour in hourly}
    readings = {stamp: r for stamp, r in hourly.items() if stamp not in whole}
    readings.update({s: r for s, r in fine.items() if s - s % hour_ms not in surrendered})

    if not readings:
        import_entity, export_entity = settings.require_entities()
        raise DataError(
            f"no statistics for {import_entity} / {export_entity} "
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
