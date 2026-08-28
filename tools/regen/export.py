#!/usr/bin/env python3
"""Regenerate the vendored NBT export-rate matrices from PG&E's upstream archive.

PG&E publishes 20 years of hourly export rates per vintage as CPUC Resolution
E-5301 requires. Each CSV is ~40 MB and ~350,640 rows, but it is a lossless
expansion of a 576-cell matrix per year (12 months x 2 day types x 24 hours)
per component. This script collapses it back down, verifying losslessness as it
goes, and emits one gzipped JSON per vintage plus the holiday calendar.

    python -m tools.regen.export --zip /path/to/PGE-Solar-Billing-Plan-Export-Rates.zip
    python -m tools.regen.export --download
    python -m tools.regen.export --download --check    # diff only, don't write

The upstream archive is not vendored (843 MB uncompressed); only the normalized
outputs under src/tariffkit/data/ are committed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sys
import zipfile
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .emit import DATA_DIR, DEFAULT_CACHE, REPO_ROOT

# Shared with the other regenerators rather than redefined. This module used
# to carry its own pair, correct while it lived at tools/regen_data.py -- two
# parents really was the repo root there -- and silently wrong once it moved
# two levels deeper into the package. DATA_DIR then pointed at
# src/tariffkit/src/tariffkit/data, so every file read as missing, --check
# reported the whole dataset stale on every run, and a write created a nested
# tree beside the real one instead of updating it.
EXPORT_DIR = DATA_DIR / "export" / "pge"

SOURCE_URL = "https://www.pge.com/assets/pge/docs/vanities/PGE-Solar-Billing-Plan-Export-Rates.zip"
PACIFIC = ZoneInfo("America/Los_Angeles")

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_INDEX = {name: i for i, name in enumerate(MONTHS)}
DAY_TYPES = ("Weekday", "Weekend")
DAY_TYPE_INDEX = {name: i for i, name in enumerate(DAY_TYPES)}

# RIN component segment -> our component name. Per the upstream readme:
#   USCA-PGXX-* = Delivery   (applies to every SBP customer)
#   USCA-XXPG-* = Generation (applies only to PG&E-bundled generation customers)
COMPONENT_BY_RIN_SEGMENT = {"PGXX": "delivery", "XXPG": "generation"}
COMPONENTS = ("generation", "delivery")

HOLIDAY_DAY_CODE = "8"

# Column positions in the 13-column CSV; see EXPECTED_HEADER below for the names.
C_RIN, C_RATE_NAME, C_DATE_START, C_TIME_START = 0, 1, 2, 3
C_DAY_START, C_VALUE_NAME, C_VALUE = 6, 8, 9

EXPECTED_HEADER = [
    "RIN",
    "RateName",
    "DateStart",
    "TimeStart",
    "DateEnd",
    "TimeEnd",
    "DayStart",
    "DayEnd",
    "ValueName",
    "Value",
    "Unit",
    "RateType",
    "Sector",
]


class NormalizationError(RuntimeError):
    """The upstream file did not match the structure we rely on."""


def parse_utc(date_str: str, time_str: str) -> datetime:
    """Parse the upstream's unpadded UTC date/time (``1/1/2026``, ``8:00:00``)."""
    month, day, year = (int(p) for p in date_str.split("/"))
    hour, minute, second = (int(p) for p in time_str.split(":"))
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def blank_matrix() -> list[list[list[float | None]]]:
    """12 months x 2 day types x 24 hours, unfilled."""
    return [[[None] * 24 for _ in DAY_TYPES] for _ in MONTHS]


def normalize_vintage(raw: bytes, member: str) -> dict[str, object]:
    """Collapse one vintage CSV into per-year 576-cell matrices.

    Returns the payload dict plus the holiday dates observed in this file.
    """
    # utf-8-sig: the header line carries a UTF-8 BOM, so a naive read yields
    # "﻿RIN" and every column lookup misses.
    reader = csv.reader(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline=""))
    header = next(reader)
    if header != EXPECTED_HEADER:
        raise NormalizationError(f"{member}: unexpected header {header!r}")

    # (year, component) -> month -> daytype -> hour -> value
    matrices: dict[tuple[int, str], list[list[list[float | None]]]] = defaultdict(blank_matrix)
    holidays: set[date] = set()
    # Years where our lookup rule reproduces upstream's own ValueName labels for
    # every row. See _exact_through for why this is not always all of them.
    label_agrees: dict[int, bool] = defaultdict(lambda: True)
    rins: dict[str, str] = {}
    rate_names: set[str] = set()
    units: set[str] = set()
    conflicts: list[str] = []
    rows = 0

    for row in reader:
        if not row:
            continue
        rows += 1
        rin = row[C_RIN]
        segment = rin.split("-")[1]
        component = COMPONENT_BY_RIN_SEGMENT.get(segment)
        if component is None:
            raise NormalizationError(f"{member}: unrecognized RIN segment in {rin!r}")
        rins[component] = rin
        rate_names.add(row[C_RATE_NAME])
        units.add(row[10])

        # The row's timestamps are UTC but its day type and ValueName are Pacific
        # Prevailing Time. Bucketing by the UTC year drags the last 8 Pacific
        # hours of December 31 into the following year and corrupts those cells,
        # so convert before bucketing.
        pacific = parse_utc(row[C_DATE_START], row[C_TIME_START]).astimezone(PACIFIC)

        if row[C_DAY_START] == HOLIDAY_DAY_CODE:
            holidays.add(pacific.date())

        month_name, day_type, hour_label = row[C_VALUE_NAME].split()
        month = MONTH_INDEX[month_name]
        day_type_idx = DAY_TYPE_INDEX[day_type]
        hour = int(hour_label.removeprefix("HS"))

        # Cross-check upstream's label against what we would derive from the
        # timestamp, using their own holiday flag. The library looks values up
        # by derived key, so any disagreement means a wrong price for that hour.
        derived_day_type = (
            "Weekend"
            if (pacific.weekday() >= 5 or row[C_DAY_START] == HOLIDAY_DAY_CODE)
            else "Weekday"
        )
        derived = (MONTHS[pacific.month - 1], derived_day_type, pacific.hour + pacific.fold)
        if derived != (month_name, day_type, hour):
            label_agrees[pacific.year] = False
        # Parse as float: the same rate is written "0.00080" in one vintage file
        # and "0.0008" in another, so string comparison reports phantom diffs.
        value = float(row[C_VALUE])

        cell = matrices[(pacific.year, component)]
        existing = cell[month][day_type_idx][hour]
        if existing is None:
            cell[month][day_type_idx][hour] = value
        elif existing != value:
            conflicts.append(
                f"{pacific.year} {component} {row[C_VALUE_NAME]}: {existing} != {value}"
            )

    if conflicts:
        raise NormalizationError(
            f"{member}: matrix is not lossless, {len(conflicts)} conflicting cells; "
            f"first: {conflicts[0]}"
        )
    if len(rate_names) != 1:
        raise NormalizationError(f"{member}: expected one RateName, got {sorted(rate_names)}")
    if sorted(rins) != sorted(COMPONENTS):
        raise NormalizationError(f"{member}: expected both components, got {sorted(rins)}")

    # Boundary years are partial: the file spans 20 years in UTC, so the first
    # and last Pacific years are clipped. Keep only years where all 576 cells
    # are populated for both components, so a lookup can never hit a None.
    data: dict[str, dict[str, list[list[list[float]]]]] = {}
    for year in sorted({year for year, _ in matrices}):
        cells = [matrices.get((year, component)) for component in COMPONENTS]
        if any(cell is None or not _is_complete(cell) for cell in cells):
            continue
        data[str(year)] = {
            component: _require_complete(matrices[(year, component)]) for component in COMPONENTS
        }

    years = [int(y) for y in data]
    payload: dict[str, object] = {
        "schema": 1,
        "vintage": next(iter(rate_names)),
        "rins": rins,
        "unit": next(iter(units)),
        "exact_through": _exact_through(years, label_agrees),
        # This vintage's own holiday flags. Kept per-vintage rather than pooled:
        # in far-future years the files disagree, with NBT25/26/00 duplicating
        # some holidays onto the following day. Using another vintage's calendar
        # would silently read the wrong column of this vintage's matrix.
        "holidays": {
            str(year): sorted(d.isoformat() for d in holidays if d.year == year)
            for year in sorted({d.year for d in holidays})
        },
        "months": list(MONTHS),
        "day_types": list(DAY_TYPES),
        "note": (
            "data[year][component][month_index][day_type_index][hour] -> $/kWh. "
            "Months and hours are Pacific Prevailing Time; values are already "
            "DST-adjusted upstream. Total export credit = generation + delivery "
            "(generation applies only to PG&E-bundled customers)."
        ),
        "years": years,
        "data": data,
    }
    return {"payload": payload, "holidays": holidays, "rows": rows}


def _exact_through(years: list[int], label_agrees: dict[int, bool]) -> int:
    """Last year whose rows all round-trip through our lookup rule.

    Upstream's hour labels stop tracking Pacific daylight time from 2036 onward
    -- the same absolute year in all five vintage files, so it is their
    generator rather than a per-file offset. Those years sit far beyond any
    nine-year rate lock and PG&E already publishes them for illustration only,
    but we record the boundary rather than let it pass silently.
    """
    exact = years[0] - 1
    for year in years:
        if not label_agrees.get(year, True):
            break
        exact = year
    return exact


def _is_complete(matrix: list[list[list[float | None]]] | None) -> bool:
    if matrix is None:
        return False
    return all(value is not None for month in matrix for day in month for value in day)


def _require_complete(matrix: list[list[list[float | None]]]) -> list[list[list[float]]]:
    """Narrow a verified-complete matrix, dropping the Optional."""
    return [[[v for v in day if v is not None] for day in month] for month in matrix]


def fetch_zip(cache: Path) -> Path:
    """Download the upstream archive, caching it outside the repo tree."""
    import httpx

    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        print(f"using cached archive {cache}")
        return cache
    print(f"downloading {SOURCE_URL}")
    with httpx.stream("GET", SOURCE_URL, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with cache.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    return cache


def vintage_slug(rate_name: str) -> str:
    return rate_name.lower()


def intersect_holidays(by_vintage: dict[str, set[date]]) -> dict[int, list[date]]:
    """Per-year intersection across the vintages that cover that year.

    Every vintage agrees on the canonical eight holidays through 2036. Later
    years pick up per-file artifacts, and intersecting removes them.
    """
    years: set[int] = {d.year for dates in by_vintage.values() for d in dates}
    result: dict[int, list[date]] = {}
    for year in sorted(years):
        covering = [
            {d for d in dates if d.year == year}
            for dates in by_vintage.values()
            if any(d.year == year for d in dates)
        ]
        common = set.intersection(*covering) if covering else set()
        if common:
            result[year] = sorted(common)
    return result


def write_holidays(holidays: dict[int, list[date]], check: bool) -> bool:
    lines = [
        "# Generated by `python -m tools.regen.export` -- do not edit by hand.",
        "#",
        "# Extracted from the DayStart==8 rows of PG&E's NBT export-rate files, so",
        "# the observed-date rules (Saturday -> preceding Friday, Sunday -> following",
        "# Monday) come from the source rather than being reimplemented here.",
        "# These eight holidays affect the EXPORT day type only; E-ELEC import",
        "# pricing makes no weekday/weekend/holiday distinction at all.",
        "#",
        "# This is the INTERSECTION across the vintage files covering each year.",
        "# They agree through 2036; beyond that NBT25/26/00 duplicate some holidays",
        "# onto the following day, and intersecting drops those artifacts. Export",
        "# lookups use the per-vintage calendar embedded in each matrix instead.",
        "",
        "[holidays]",
    ]
    for year in sorted(holidays):
        dates = ", ".join(f'"{d.isoformat()}"' for d in sorted(holidays[year]))
        lines.append(f"{year} = [{dates}]")
    content = "\n".join(lines) + "\n"

    target = DATA_DIR / "holidays.toml"
    if check:
        current = target.read_text() if target.exists() else ""
        if current != content:
            print(f"CHANGED: {target.relative_to(REPO_ROOT)}")
            return True
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    print(f"wrote {target.relative_to(REPO_ROOT)} ({len(holidays)} years)")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--zip", type=Path, help="path to a local copy of the archive")
    source.add_argument("--download", action="store_true", help="fetch the archive from PG&E")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the vendored data is stale without writing anything",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE / "PGE-Solar-Billing-Plan-Export-Rates.zip",
    )
    args = parser.parse_args(argv)

    archive = fetch_zip(args.cache) if args.download else args.zip
    if not archive.exists():
        parser.error(f"archive not found: {archive}")

    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"archive sha256 {archive_sha}")

    holidays_by_vintage: dict[str, set[date]] = {}
    manifest_vintages: dict[str, dict[str, object]] = {}
    stale = False

    with zipfile.ZipFile(archive) as zf:
        members = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
        if not members:
            raise NormalizationError("archive contains no CSV members")
        for member in members:
            raw = zf.read(member)
            member_sha = hashlib.sha256(raw).hexdigest()
            result = normalize_vintage(raw, member)
            payload = result["payload"]
            assert isinstance(payload, dict)
            holidays = result["holidays"]
            assert isinstance(holidays, set)

            rate_name = str(payload["vintage"])
            holidays_by_vintage[rate_name] = holidays
            payload["source"] = {
                "url": SOURCE_URL,
                "member": member,
                "member_sha256": member_sha,
                "archive_sha256": archive_sha,
            }
            slug = vintage_slug(rate_name)
            target = EXPORT_DIR / f"{slug}.json.gz"
            # mtime=0 so identical input always produces identical bytes, which
            # is what makes --check a meaningful diff.
            body = gzip.compress(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(), mtime=0
            )

            years = payload["years"]
            assert isinstance(years, list)
            exact_through = payload["exact_through"]
            print(
                f"{member}: {result['rows']} rows -> {len(years)} complete years "
                f"({years[0]}-{years[-1]}), exact through {exact_through}, "
                f"{len(body) / 1024:.0f} KiB gzipped"
            )

            manifest_vintages[rate_name] = {
                "file": f"export/pge/{slug}.json.gz",
                "member": member,
                "member_sha256": member_sha,
                "years": [years[0], years[-1]],
                "exact_through": exact_through,
            }

            if args.check:
                if not target.exists() or target.read_bytes() != body:
                    print(f"CHANGED: {target.relative_to(REPO_ROOT)}")
                    stale = True
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)

    stale |= write_holidays(intersect_holidays(holidays_by_vintage), args.check)

    manifest = {
        "source_url": SOURCE_URL,
        "archive_sha256": archive_sha,
        "vintages": manifest_vintages,
    }
    # Not "manifest.json": hacs/default rejects a repository that contains
    # more than one *manifest.json, which would collide with the Home
    # Assistant manifest in custom_components/tariffkit.
    manifest_path = DATA_DIR / "sources.json"
    manifest_body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = manifest_path.read_text() if manifest_path.exists() else ""
        if current != manifest_body:
            print(f"CHANGED: {manifest_path.relative_to(REPO_ROOT)}")
            stale = True
        if stale:
            print("\nVendored data is out of date with upstream.")
            return 1
        print("\nVendored data matches upstream.")
        return 0

    manifest_path.write_text(manifest_body)
    print(f"wrote {manifest_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
