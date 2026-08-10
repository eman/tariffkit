"""Read interval data from CSV.

Deliberately stdlib-only, so the billing path stays dependency-free. Column
names vary by source (PG&E Green Button exports, Home Assistant statistics
dumps, inverter logs), so they are configurable rather than guessed.
"""

from __future__ import annotations

import csv
import re
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
#: Split date/time pairs, as PG&E's interval export uses. A matching pair takes
#: precedence over a single timestamp column, because PG&E's time column is named
#: "START TIME" and would otherwise be mistaken for a whole timestamp. A date
#: column with no time column beside it falls back to being read as one.
DATE_CANDIDATES = ("date", "usage_date", "read_date", "interval_date")
TIME_CANDIDATES = ("start_time", "time", "interval_start_time", "hour")


def _normalize(name: str) -> str:
    """Fold case, punctuation, and units so header spellings converge.

    ``"IMPORT (kWh)"`` and ``"import_kwh"`` are the same column; so are
    ``"START TIME"`` and ``"start_time"``. Matching on the raw string means every
    new export format needs its own candidate entry.
    """
    return re.sub(r"[^0-9a-z]+", "_", name.strip().lower()).strip("_")


@dataclass(frozen=True, slots=True)
class CsvLayout:
    """Which columns hold what.

    Leave fields as ``None`` to auto-detect from the header.
    """

    start: str | None = None
    #: Split date/time pair. Setting both takes precedence over ``start``; set
    #: neither and a pair is still auto-detected ahead of a single column.
    date: str | None = None
    time: str | None = None
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
    normalized = {_normalize(name): name for name in header if name}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _skip_preamble(handle: IO[str]) -> Iterator[str]:
    """Yield lines from the real header row onward.

    PG&E's interval export opens with account metadata (name, address, account
    and service numbers) and a blank line before the column header, so feeding
    the file straight to ``DictReader`` picks up ``"Name,EMMANUEL ..."`` as the
    header. Scan for the first row carrying a recognisable timestamp column
    instead of assuming a fixed offset, since the preamble's length is not
    documented and has changed before.
    """
    wanted = set(START_CANDIDATES) | set(DATE_CANDIDATES)
    lines = handle.read().splitlines()
    for index, row in enumerate(csv.reader(lines)):
        if any(_normalize(cell) in wanted for cell in row):
            yield from lines[index:]
            return
    # No recognisable header: hand back the file unchanged so the existing
    # "no timestamp column" error names the columns actually present.
    yield from lines


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
    reader = csv.DictReader(_skip_preamble(handle))
    header = reader.fieldnames
    if not header:
        raise DataError("CSV has no header row")
    header = list(header)

    date_col = _pick(header, layout.date, DATE_CANDIDATES)
    time_col = _pick(header, layout.time, TIME_CANDIDATES)
    start_col = _pick(header, layout.start, START_CANDIDATES)

    # A split date/time pair wins over a single column, because PG&E names its
    # time column "START TIME" -- which looks like a full timestamp column but
    # holds "00:15". Falling back to a lone date column keeps files that put a
    # complete ISO value under "date" working.
    paired = date_col is not None and time_col is not None and date_col != time_col
    if not paired:
        start_col = start_col or date_col
        date_col = time_col = None
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
        if date_col is not None and time_col is not None:
            day = (row.get(date_col) or "").strip()
            clock = (row.get(time_col) or "").strip()
            if not day or not clock:
                continue
            start = _parse_timestamp(f"{day}T{clock}", layout.assume_tz)
        else:
            assert start_col is not None
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
