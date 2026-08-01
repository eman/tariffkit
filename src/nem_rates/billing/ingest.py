"""Read interval data from CSV.

Deliberately stdlib-only, so the billing path stays dependency-free. Column
names vary by source (PG&E Green Button exports, Home Assistant statistics
dumps, inverter logs), so they are configurable rather than guessed.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from typing import IO, NamedTuple

from ..errors import DataError
from ..timeutil import PACIFIC
from .models import IntervalReading


class _Row(NamedTuple):
    """One parsed CSV row, before interval length is known."""

    start: datetime
    imported: float | None
    exported: float | None
    net: float | None


#: Column names seen in the wild, tried in order when none is configured.
START_CANDIDATES = (
    "start",
    "start_time",
    "timestamp",
    "datetime",
    "date_time",
    "time",
    "last_reset",
)
IMPORT_CANDIDATES = ("imported", "import", "import_kwh", "delivered", "consumption", "usage", "kwh")
EXPORT_CANDIDATES = ("exported", "export", "export_kwh", "received", "surplus", "production")
NET_CANDIDATES = ("net", "net_kwh", "net_usage")


@dataclass(frozen=True, slots=True)
class CsvLayout:
    """Which columns hold what.

    Leave fields as ``None`` to auto-detect from the header.
    """

    start: str | None = None
    imported: str | None = None
    exported: str | None = None
    #: Signed column, positive meaning import. Alternative to imported/exported.
    net: str | None = None
    #: Interval length. Inferred from the first two rows when omitted.
    duration: timedelta | None = None
    #: Applied to timestamps that carry no UTC offset.
    assume_tz: tzinfo = PACIFIC


def _pick(header: list[str], configured: str | None, candidates: tuple[str, ...]) -> str | None:
    if configured is not None:
        if configured not in header:
            raise DataError(f"column {configured!r} not in header: {header}")
        return configured
    lowered = {name.strip().lower(): name for name in header}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _parse_timestamp(raw: str, assume_tz: tzinfo) -> datetime:
    text = raw.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DataError(f"could not parse timestamp {raw!r} as ISO 8601") from exc
    if moment.tzinfo is None:
        # Naive timestamps are ambiguous across DST. Assume the configured zone
        # and say so, rather than silently treating them as UTC.
        return moment.replace(tzinfo=assume_tz)
    return moment


def _parse_float(raw: str | None) -> float:
    if raw is None:
        return 0.0
    text = raw.strip().replace("$", "").replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise DataError(f"could not parse {raw!r} as a number") from exc


def read_csv(
    source: str | Path | IO[str], layout: CsvLayout | None = None
) -> list[IntervalReading]:
    """Read interval readings from a CSV file or open stream."""
    layout = layout or CsvLayout()
    if isinstance(source, str | Path):
        with Path(source).open(encoding="utf-8-sig", newline="") as handle:
            return list(_read(handle, layout))
    return list(_read(source, layout))


def _read(handle: IO[str], layout: CsvLayout) -> Iterator[IntervalReading]:
    reader = csv.DictReader(handle)
    header = reader.fieldnames
    if not header:
        raise DataError("CSV has no header row")
    header = list(header)

    start_col = _pick(header, layout.start, START_CANDIDATES)
    if start_col is None:
        raise DataError(f"no timestamp column found in {header}; set CsvLayout(start=...)")

    import_col = _pick(header, layout.imported, IMPORT_CANDIDATES)
    export_col = _pick(header, layout.exported, EXPORT_CANDIDATES)
    net_col = _pick(header, layout.net, NET_CANDIDATES)

    if import_col is None and export_col is None and net_col is None:
        raise DataError(
            f"no energy column found in {header}; set CsvLayout(imported=...), "
            f"(exported=...), or (net=...)"
        )

    rows: list[_Row] = []
    for row in reader:
        if not (row.get(start_col) or "").strip():
            continue
        start = _parse_timestamp(row[start_col], layout.assume_tz)
        if net_col is not None and import_col is None and export_col is None:
            rows.append(_Row(start, None, None, _parse_float(row.get(net_col))))
        else:
            rows.append(
                _Row(
                    start,
                    _parse_float(row.get(import_col) if import_col else None),
                    _parse_float(row.get(export_col) if export_col else None),
                    None,
                )
            )

    if not rows:
        raise DataError("CSV contained no data rows")

    duration = layout.duration or _infer_duration([r.start for r in rows])

    for start, imported, exported, net in rows:
        if net is not None:
            yield IntervalReading.from_net(start, net, duration)
        else:
            yield IntervalReading(
                start, imported=imported or 0.0, exported=exported or 0.0, duration=duration
            )


def _infer_duration(timestamps: Sequence[datetime]) -> timedelta:
    """Infer interval length from the two closest consecutive timestamps.

    The minimum gap rather than the first gap, so a leading DST jump or a single
    missing row does not stretch every interval in the file.
    """
    if len(timestamps) < 2:
        return timedelta(hours=1)
    starts = sorted(timestamps)
    gaps = [later - earlier for earlier, later in pairwise(starts) if later > earlier]
    if not gaps:
        return timedelta(hours=1)
    return min(gaps)
