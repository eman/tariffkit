"""Interval readings from raw meter counters in InfluxDB 3.

Home Assistant writes each numeric sensor sample to InfluxDB, so the same
smart-meter counters land here as a plain time series of readings
rather than the pre-aggregated buckets :mod:`tariffkit.sources.homeassistant`
returns. Two consequences, and they point in opposite directions.

**Totals are exact.** Energy over a window is the counter's endpoints, so it
does not matter how densely the counter was sampled in between. Measured against
a real statement, this reproduced 39.902 kWh imported against 39.906 billed and
193.795 exported against 193.797 -- closer than PG&E's own CSV export, which
rounds every interval to two decimals and loses about 2% of a low-import month.

**Distribution across intervals is only as good as the sampling.** A sample
reports an advance since the previous one, not an instant, so energy is spread
pro rata over the span it accrued across (see :func:`_per_interval` for why
crediting it to the later sample instead is materially wrong at TOU boundaries),
except across a gap, where the rates either side say where it arrived (see
:func:`_accrual`). Spacing on this data has a median of five minutes but a 90th percentile of
three quarters of an hour, so hourly is the honest default; anything finer is
interpolation, and should be checked against the density for that period.

The default entities are the **unfiltered** counters, which is the opposite of
the Home Assistant source's default and deliberate: those go back fourteen
months against the filtered pair's five, and the drop-to-zero behaviour that
makes them unusable raw is repaired here anyway.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..account.model import MeterSource
from ..billing.models import IntervalReading
from ..config import default_config_path, default_dotenv_path
from ..errors import ConfigError, DataError
from ..metering import monotonic
from ..secrets import get_secret, load_dotenv
from ..timeutil import to_pacific

#: What 0.8.1 and earlier read when nothing named the series. Kept only to make
#: that upgrade a copy-paste; nothing reads them, and they can go once 0.8.x is
#: far enough behind.
_RETIRED_DEFAULTS: tuple[str, str] = (
    "eagle_100_total_energy_delivered",
    "eagle_100_total_energy_received",
)

#: No default pair: series names are site-specific, and the one that used to be
#: here named the hardware this was developed against. Prefer the *raw* counters
#: when you name them -- unfiltered on purpose, see the module docstring.

#: Home Assistant's InfluxDB integration writes one row per numeric sample.
DEFAULT_TABLE = "sensor_numeric"

#: How far before the window to look for a counter reading to subtract from.
#: Sampling has been as sparse as one reading every three hours, and without a
#: baseline the first interval would silently start from zero.
BASELINE_LOOKBACK = timedelta(days=3)

#: How far past the window to read, for the same reason in the other direction.
#: Where :func:`_accrual` puts a gap's energy depends on the rate just after the
#: gap, and a window ending before that rate was sampled read it as unknown: an
#: hour near the end priced differently depending on how far past it the window
#: ran. Samples past the window inform placement only; what they accrue beyond
#: ``end`` is clipped, as the baseline's span before ``start`` is.
ACCRUAL_LOOKAHEAD = BASELINE_LOOKBACK

#: Entity ids and the table name are interpolated into SQL, so they are
#: constrained rather than quoted: anything outside this is rejected instead of
#: escaped.
_ENTITY_RE = re.compile(r"^[A-Za-z0-9_.]+$")


@dataclass(frozen=True, slots=True)
class InfluxSettings:
    """Where to reach InfluxDB 3, and which series carry grid exchange."""

    host: str
    database: str
    #: Never printed. `repr=False` keeps it out of tracebacks, which render
    #: dataclass frames -- the same reason PgeSettings marks its own.
    token: str = field(repr=False)
    #: Optional: rate pricing needs neither. ``None`` means "not configured",
    #: which is answered when a read is attempted, not at load time.
    import_entity: str | None = None
    export_entity: str | None = None
    table: str = DEFAULT_TABLE

    def require_entities(self) -> tuple[str, str]:
        """The two series names, or a ConfigError naming how to set them."""
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
                f"InfluxDB {' and '.join(missing)} not set. Name your grid series:\n"
                f"  tariffkit account source set influx \\\n"
                f"    --grid-import-entity YOUR_IMPORT_SERIES \\\n"
                f"    --grid-export-entity YOUR_EXPORT_SERIES --apply\n"
                f"or put import_entity/export_entity under [influxdb] in the config "
                f"file.\n\n"
                f"Upgrading from 0.8.1 or earlier? Those versions read "
                f"{_RETIRED_DEFAULTS[0]} and {_RETIRED_DEFAULTS[1]} when nothing "
                f"named them, so if you never configured these, that is what you "
                f"were using.\n\n"
                f"Rate pricing (`tariffkit now`, `forecast`, `info`) needs neither."
            )
        assert self.import_entity is not None and self.export_entity is not None
        return self.import_entity, self.export_entity

    @property
    def query_url(self) -> str:
        base = self.host if "://" in self.host else f"https://{self.host}"
        return f"{base.rstrip('/')}/api/v3/query_sql"

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        dotenv_path: str | Path | None = None,
        profile_source: MeterSource | None = None,
        **overrides: str | None,
    ) -> InfluxSettings:
        """Resolve from config file, ``.env``, environment, profile, then args.

        Same split as the Home Assistant source: entity ids and the database are
        configuration and may live in the config file; a profile mapping can
        replace the grid-import/grid-export series; the token is a secret and
        is read only from ``.env`` or the environment.
        """
        import os

        values: dict[str, str] = {}
        path = Path(config_path) if config_path else default_config_path()
        if path.is_file():
            table = tomllib.loads(path.read_text(encoding="utf-8")).get("influxdb", {})
            for key in ("host", "database", "import_entity", "export_entity", "table"):
                if key in table:
                    values[key] = str(table[key])

        env = {**load_dotenv(dotenv_path), **os.environ}
        for key, name in (
            ("host", "INFLUXDB3_HOST"),
            ("database", "INFLUXDB3_DATABASE"),
            ("token", "INFLUXDB3_AUTH_TOKEN"),
            ("import_entity", "TARIFFKIT_INFLUX_IMPORT_ENTITY"),
            ("export_entity", "TARIFFKIT_INFLUX_EXPORT_ENTITY"),
        ):
            if value := env.get(name):
                values[key] = value
        if "token" not in values and (token := get_secret("influxdb.token")):
            values["token"] = token
        if profile_source is not None:
            if not isinstance(profile_source, MeterSource):
                raise ConfigError("profile_source must be a MeterSource")
            values["import_entity"] = profile_source.grid_import_entity
            values["export_entity"] = profile_source.grid_export_entity
        values.update({k: v for k, v in overrides.items() if v})

        missing = [k for k in ("host", "database", "token") if not values.get(k)]
        if missing:
            raise ConfigError(
                f"InfluxDB {', '.join(missing)} not set; put INFLUXDB3_HOST, "
                f"INFLUXDB3_DATABASE and INFLUXDB3_AUTH_TOKEN in "
                f"{Path(dotenv_path) if dotenv_path else default_dotenv_path()}, the "
                f"environment, or store influxdb.token with "
                f"`tariffkit credentials set`"
            )
        return cls(
            host=values["host"],
            database=values["database"],
            token=values["token"],
            import_entity=_clean_entity(values["import_entity"])
            if values.get("import_entity")
            else None,
            export_entity=_clean_entity(values["export_entity"])
            if values.get("export_entity")
            else None,
            table=_sql_name(values.get("table", DEFAULT_TABLE), "table name"),
        )


def _clean_entity(entity: str) -> str:
    """Accept either ``sensor.foo`` or ``foo``; InfluxDB stores the latter."""
    name = entity.split(".", 1)[1] if entity.startswith("sensor.") else entity
    return _sql_name(name, "entity id")


def _sql_name(name: str, what: str) -> str:
    """Guard an identifier that will be interpolated into SQL.

    Both the entity ids and the table name reach the query as text rather than
    bound parameters, and both can come from a config file. Constrain rather
    than escape: a name outside this alphabet is a mistake, not something to
    quote around.
    """
    if not _ENTITY_RE.match(name):
        raise ConfigError(f"unsupported {what} {name!r}")
    return name


def _query(settings: InfluxSettings, sql: str) -> list[dict[str, Any]]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised by packaging
        raise RuntimeError(
            "the InfluxDB source requires the 'influx' extra: pip install 'tariffkit[influx]'"
        ) from exc

    try:
        response = httpx.post(
            settings.query_url,
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Content-Type": "application/json",
            },
            json={"db": settings.database, "q": sql, "format": "json"},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        # A timeout or a refused connection is the database being unreachable,
        # not a programming error. Left as httpx's own exception it escaped
        # every caller's handler as a traceback, and `audit run` exited 1 --
        # the code for a billing discrepancy -- when nothing had been compared.
        raise DataError(
            f"could not query InfluxDB at {settings.host}: {type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code != 200:
        raise DataError(
            f"InfluxDB refused the query ({response.status_code}): {response.text[:200]}"
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise DataError(f"unexpected InfluxDB response: {str(payload)[:200]}")
    return payload


def _samples(
    settings: InfluxSettings, entity: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    # Guard here as well as in load(): settings can be constructed directly, and
    # nothing downstream would notice a name that is not an identifier.
    sql = (
        f"SELECT time, value FROM {_sql_name(settings.table, 'table name')} "
        f"WHERE entity_id = '{_sql_name(entity, 'entity id')}' "
        f"AND time >= '{start.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}' "
        f"AND time <= '{end.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}' "
        f"ORDER BY time"
    )
    rows = _query(settings, sql)
    out: list[tuple[datetime, float]] = []
    for row in rows:
        raw = row.get("time")
        value = row.get("value")
        if raw is None or value is None:
            continue
        try:
            moment = datetime.fromisoformat(str(raw))
            reading = float(value)
        except (TypeError, ValueError) as exc:
            # A row we cannot read is a wire-format problem, not a programming
            # error; say which row rather than letting a bare ValueError out.
            raise DataError(
                f"could not read a {entity} sample from InfluxDB "
                f"(time={raw!r}, value={value!r}): {exc}"
            ) from exc
        # InfluxDB stores UTC and returns it without an offset, so a naive
        # timestamp is UTC rather than local.
        out.append((moment if moment.tzinfo else moment.replace(tzinfo=UTC), reading))
    return out


#: A sample gap wider than this makes the *shape* of what it spans a guess.
#: Spreading it evenly is still the best available estimate of the total, which
#: a cumulative counter fixes exactly at the endpoints, but the split between
#: intervals stops being measured and starts being assumed. Chosen at one hour
#: because that is the granularity a time-of-use tariff prices at: a gap inside
#: one interval cannot move energy across a rate boundary, and a gap spanning
#: several can.
SMEARED_GAP = timedelta(hours=1)

#: Below this much smeared energy an interval is not worth doubting, in kWh.
#:
#: The doubt is about money: a kilowatt-hour put in the wrong time-of-use band
#: is mispriced by the spread between the bands, and on this tariff the widest
#: export spread is about $0.50. Ten watt-hours is therefore half a cent at the
#: very worst, and no arrangement of it changes a bill.
#:
#: Without a floor the warning fired on every interval a wide gap touched at
#: all, which on a real cycle was 453 of 744 -- 61% of it flagged over 0.83 kWh
#: whose largest single share was 0.083. A warning that broad is one its reader
#: learns to skip, and the warnings beside it are not skippable.
SMEARED_FLOOR = 0.01


#: Spacing up to this is the source's ordinary cadence, and an advance across
#: it is spread pro rata. Home Assistant publishes these counters about every
#: five minutes while they move, with a few minutes' jitter either side.
REGULAR_SPACING = timedelta(minutes=10)

#: How much ordinary sampling a rate is read from, on either side of a gap.
#: About one cadence: enough to step over the occasional sample that lands
#: seconds after another -- one 31-second pair after an outage implied 26 kW --
#: and short enough to still be the rate at the gap's edge.
RATE_WINDOW = timedelta(minutes=4)

#: The least sampling a rate is read from at all.
MIN_RATE_SPAN = timedelta(minutes=1)


def _edge_rate(
    points: Sequence[tuple[datetime, float]], index: int, direction: int
) -> float | None:
    """kWh per second over the ordinary sampling beside a gap, or None.

    ``index`` is the sample at the gap's edge, and ``direction`` is -1 to read
    the samples before it or +1 for those after. Stops at the next pair wider
    than :data:`REGULAR_SPACING`, because a second gap is not a rate.
    """
    advance = 0.0
    span = 0.0
    while span < RATE_WINDOW.total_seconds():
        other = index + direction
        if other < 0 or other >= len(points):
            break
        early, late = sorted((index, other))
        seconds = (points[late][0] - points[early][0]).total_seconds()
        if seconds > REGULAR_SPACING.total_seconds():
            break
        advance += points[late][1] - points[early][1]
        span += seconds
        index = other
    return advance / span if span >= MIN_RATE_SPAN.total_seconds() else None


def _accrual(
    was_at: datetime,
    instant: datetime,
    advance: float,
    before: float | None,
    after: float | None,
) -> list[tuple[datetime, datetime, float]]:
    """Where in ``[was_at, instant]`` an advance across a gap arrived.

    Pieces of ``(start, end, kWh)``, each spread pro rata over its own span.

    Spreading the whole advance evenly across a gap treats every minute of it
    as equally likely to have carried the energy, and that is the least likely
    reading there is. Home Assistant publishes a counter's state when it
    changes, so a gap is usually a counter standing still: across the
    2026-08-28 cycle, gaps of twelve minutes or more advanced a median of one
    watt-hour. The energy that ends one arrived at its end -- and its end is
    often a step change. A battery set to export from 19:00 last published at
    18:35 and next at 19:05, +0.208 kWh, and spreading that put 0.17 kWh in the
    18:00 hour where PG&E's own 15-minute data has none. It happened on almost
    every evening of the cycle, at an hourly export value that falls from about
    $0.97 to $0.59 across the boundary, and accounted for most of the cycle's
    $0.53 shortfall in MCE export credit.

    So two readings are tried, in order of how much the samples support them:

    1. **The counter stood still, then moved at the rate it moved at after the
       gap.** That is the only history consistent with a live source publishing
       nothing, and it places the advance in the stretch just before ``instant``.
    2. **The source was away, and the energy kept flowing.** When the rate after
       the gap is too low to account for the advance inside it, nothing was
       publishing for at least part of the gap. The rate before the gap is taken
       to have continued, switching to the rate after at the one instant that
       makes the advance add up. A battery exporting at 2.4 kW when the samples
       stopped for 35 minutes, and idle when they resumed, finished its export
       inside the gap rather than spread across the hour after it.

    Neither fits -- no rate either side, or an advance no switch between them
    explains -- and the advance is spread evenly, which is what was done for
    every gap before. On every cycle with PG&E's 15-minute data to check it
    against, this moved the hourly split closer to that data, or held it
    within 0.6 kWh.
    """
    span = (instant - was_at).total_seconds()
    if span > REGULAR_SPACING.total_seconds():
        if after:
            needed = advance / after
            if needed <= span:
                return [(instant - timedelta(seconds=needed), instant, advance)]
        if before is not None and after is not None and before != after:
            # before * x + after * (span - x) = advance, for the seconds x the
            # earlier rate ran before the later one took over.
            ran = (advance - after * span) / (before - after)
            if 0.0 <= ran <= span:
                switch = was_at + timedelta(seconds=ran)
                return [(was_at, switch, before * ran), (switch, instant, advance - before * ran)]
    return [(was_at, instant, advance)]


def _per_interval(
    samples: list[tuple[datetime, float]], start: datetime, end: datetime, step: timedelta
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """Counter advance per interval, spread across the span it accrued over.

    A sample says only that the counter advanced by some amount *since the
    previous sample*, not that the energy arrived at the instant of reading.
    Crediting the whole advance to the interval holding the later sample biases
    energy forward across every boundary it spans, and boundaries are where the
    money is: the export delivery credit is roughly 500x larger during the 4-9pm
    peak than outside it. On a real July statement that rule put 55.52 kWh of
    export in peak where PG&E's own 15-minute data has 52.08; spreading pro rata
    gives 52.62. Median sample spacing is five minutes, but the 90th percentile
    is three quarters of an hour, so the error is not marginal.

    Pro rata is right across the ordinary cadence, and wrong across a gap, where
    the energy almost never arrived evenly; :func:`_accrual` decides where it
    did.

    Advance that accrued before ``start`` or after ``end`` is clipped rather
    than counted, so a baseline sample reaching back days does not dump its
    whole span into the first interval, nor a lookahead sample into the last.

    Intervals are stepped in absolute time so the two DST transitions produce
    23 and 25 hour days rather than being assumed 24.
    """
    edges: list[datetime] = []
    cursor = start.astimezone(UTC)
    limit = end.astimezone(UTC)
    while cursor < limit:
        edges.append(cursor)
        cursor += step
    if not edges:
        return {}, {}

    totals = dict.fromkeys(edges, 0.0)
    # How much of each interval came out of a gap wider than the interval, not
    # merely whether one touched it. A boolean overstated the doubt enormously:
    # an hour taking 0.001 kWh from a wide gap and 5 kWh from dense samples was
    # flagged the same as one that was entirely guesswork.
    #
    # Still judged by the gap, not by how narrowly `_accrual` placed what it
    # carried. Placing it is an inference from the rates either side, and an
    # inference about where energy fell across an hour is exactly what this
    # records; narrowing the placement does not make it measured.
    smeared: dict[datetime, float] = {}
    points = [(moment.astimezone(UTC), value) for moment, value in samples]
    for position in range(1, len(points)):
        was_at, was = points[position - 1]
        instant, value = points[position]
        advance = value - was
        span = instant - was_at
        if advance <= 0 or span <= timedelta(0) or instant <= edges[0]:
            continue
        if span > REGULAR_SPACING:
            pieces = _accrual(
                was_at,
                instant,
                advance,
                _edge_rate(points, position - 1, -1),
                _edge_rate(points, position, +1),
            )
        else:
            pieces = [(was_at, instant, advance)]
        for begins, ends, energy in pieces:
            length = (ends - begins).total_seconds()
            if energy <= 0 or length <= 0:
                continue
            # Walk only the intervals the piece actually touches. Indices are
            # arithmetic because intervals are uniform in absolute time. Capped
            # at `limit` as well as at the last interval: a window that is not
            # a whole number of steps ends inside its last interval, and the
            # lookahead samples past it must not fill the rest.
            lower = max(begins, edges[0])
            stop = min(ends, limit)
            while lower < stop:
                index = int((lower - edges[0]) // step)
                if index >= len(edges):
                    break
                upper = min(stop, edges[index] + step)
                share = energy * (upper - lower).total_seconds() / length
                totals[edges[index]] += share
                if span > SMEARED_GAP:
                    smeared[edges[index]] = smeared.get(edges[index], 0.0) + share
                lower = upper
    return totals, smeared


def read_counters(
    settings: InfluxSettings,
    start: datetime,
    end: datetime,
    resolution: timedelta = timedelta(hours=1),
) -> list[IntervalReading]:
    """Interval readings for ``[start, end)`` from raw counter samples.

    Totals over the window are exact regardless of sampling density, because a
    cumulative counter only depends on its endpoints. How that energy divides
    between intervals does depend on density; see the module docstring.
    """
    for name, moment in (("start", start), ("end", end)):
        if moment.tzinfo is None:
            raise ConfigError(f"{name} must be timezone-aware; got {moment.isoformat()}")
    if end <= start:
        raise ConfigError(f"end {end.isoformat()} is not after start {start.isoformat()}")
    if resolution <= timedelta(0):
        raise ConfigError(f"resolution must be positive, got {resolution}")

    # Reach back before the window so the first interval has something to
    # subtract from; otherwise it would silently start from zero. Reach past it
    # so a gap near the end is placed from the rate after it, and so a gap
    # straddling the end contributes its share inside the window rather than
    # none of it.
    lookback = start - BASELINE_LOOKBACK
    lookahead = end + ACCRUAL_LOOKAHEAD
    import_entity, export_entity = settings.require_entities()
    import_samples = monotonic(_samples(settings, import_entity, lookback, lookahead))
    export_samples = monotonic(_samples(settings, export_entity, lookback, lookahead))
    # Test the samples, not the bucketed result: bucketing always yields an
    # entry per interval, so an empty window is indistinguishable from a quiet
    # one once it has been through _per_interval. Lookahead samples do not
    # count: with nothing at or before `end`, the window has no reading to
    # measure from, and samples after it would otherwise pass it off as a
    # measured zero.
    if not any(moment <= end for moment, _ in (*import_samples, *export_samples)):
        raise DataError(
            f"no samples for {import_entity} / {export_entity} "
            f"between {start.isoformat()} and {end.isoformat()}"
        )
    imported, smeared_in = _per_interval(import_samples, start, end, resolution)
    exported, smeared_out = _per_interval(export_samples, start, end, resolution)

    def spread(edge: datetime) -> float:
        return max(smeared_in.get(edge, 0.0), 0.0) + max(smeared_out.get(edge, 0.0), 0.0)

    # Flagged on the energy, not on a gap having passed through. Most wide gaps
    # here are the counter standing still overnight, where spreading nothing
    # across them guesses nothing -- and calling those intervals reconstructed
    # put 61% of a cycle under a warning that 0.2% of it deserved.
    return [
        IntervalReading(
            start=to_pacific(edge),
            imported=max(imported.get(edge, 0.0), 0.0),
            exported=max(exported.get(edge, 0.0), 0.0),
            duration=resolution,
            estimated=spread(edge) >= SMEARED_FLOOR,
            # Recorded whatever its size. The floor decides whether *this*
            # interval is worth calling reconstructed; it must not decide
            # whether the energy existed, because a share too small to matter
            # on its own still adds up across a cycle and the reader is owed
            # the total. `check_coverage` applies materiality after summing.
            smeared=spread(edge),
        )
        for edge in sorted(set(imported) | set(exported))
    ]
