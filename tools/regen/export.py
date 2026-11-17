#!/usr/bin/env python3
"""Regenerate the vendored NBT export-rate matrices from PG&E's upstream archive.

PG&E publishes 20 years of hourly export rates per vintage as CPUC Resolution
E-5301 requires. Each CSV is ~40 MB and ~350,640 rows, but it is a lossless
expansion of a 576-cell matrix per year (12 months x 2 day types x 24 hours)
per component. This script collapses it back down, verifying losslessness as it
goes, and emits one gzipped JSON per vintage plus the holiday calendar.

    python -m tools.regen.export --zip /path/to/PGE-Solar-Billing-Plan-Export-Rates.zip
    python -m tools.regen.export --download              # reuses a cached archive
    python -m tools.regen.export --download --refresh    # fetches it again
    python -m tools.regen.export --download --check      # diff only, don't write

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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tariffkit.timeutil import FALL_BACK_LABELS, is_repeated_hour

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
    # Pacific year -> the hour labels upstream gave the repeated 01:00 PST on
    # the fall-back day. See FALL_BACK_LABELS for why this is read, not assumed.
    fall_back_labels: dict[int, set[int]] = defaultdict(set)
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
        if is_repeated_hour(pacific):
            # Which label the repeated hour carries is the file's convention
            # rather than something derivable, so it is recorded here and
            # checked once the whole year has been seen.
            fall_back_labels[pacific.year].add(hour)
            derived_hour = hour
        else:
            derived_hour = pacific.hour
        derived = (MONTHS[pacific.month - 1], derived_day_type, derived_hour)
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
    fall_back_hour: dict[str, int] = {}
    for year in years:
        labels = fall_back_labels.get(year, set())
        if len(labels) == 1 and (label := next(iter(labels))) in FALL_BACK_LABELS:
            fall_back_hour[str(year)] = label
        else:
            # Neither convention, or the two components disagree: no rule
            # reproduces that hour, so the year is not exact. The lookup falls
            # back to the HS2 reading for it.
            label_agrees[year] = False

    exact_through = _exact_through(years, label_agrees)
    if exact_through < years[0]:
        # Not even the first year round-trips, so every price this vintage would
        # return is unverified. That is a rule to fix, not data to ship -- the
        # 2027 vintages arrived like this when PG&E changed how it labels the
        # fall-back hour, and writing them anyway would have reported every
        # price as inexact.
        raise NormalizationError(
            f"{member}: no year reproduces upstream's own labels, starting with {years[0]}"
        )

    payload: dict[str, object] = {
        "schema": 1,
        "vintage": next(iter(rate_names)),
        "rins": rins,
        "unit": next(iter(units)),
        "exact_through": exact_through,
        # Year -> the HS label of the repeated 01:00 PST on that year's
        # fall-back day. Read from the file because PG&E has used both.
        "fall_back_hour": fall_back_hour,
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

    From some year on, upstream's hour labels stop tracking Pacific daylight
    time: every daylight-time hour is labelled an hour off, starting with the
    spring-forward day. It is not one absolute year -- 2036 in the 2025
    vintage, 2037 in 2026, 2038 in 2027 and the November 2026 floating file,
    2039 in 2028 -- and the 2023 and 2024 vintages never do it. Those years sit
    beyond any nine-year rate lock and PG&E already publishes them for
    illustration only, but we record the boundary rather than let it pass
    silently.

    The repeated fall-back hour is a separate matter and does not count
    against a year: its label is recorded per year in ``fall_back_hour``.
    """
    exact = years[0] - 1
    for year in years:
        if not label_agrees.get(year, True):
            break
        exact = year
    return exact


def retain_dropped_years(
    payload: dict[str, Any], previous: dict[str, Any] | None, member: str
) -> list[int]:
    """Carry forward the leading years a newer upstream file no longer covers.

    The locked vintages are fixed 20-year series, but PG&E republishes the
    floating one each year starting from the next, and the old file goes. In
    November 2026 it began at 2027, which dropped 2025 and 2026 -- the two
    years its own readme calls "actual effective rates" for NBT00. Shipping
    that as published would have left every floating customer without an
    export price for the rest of 2026, and without one for any bill before it.

    Those years are taken from the vendored copy, with the source they were
    verified against, rather than from the new file, which never had them.
    Only years before the new file's first one are kept: a year upstream still
    publishes is always upstream's, so a correction to it is not masked.
    Returns the retained years, oldest first, after updating ``payload``.
    """
    if previous is None:
        return []
    years: list[int] = payload["years"]
    retained = [y for y in previous["years"] if y < years[0]]
    if not retained:
        return []
    if retained[-1] != years[0] - 1:
        raise NormalizationError(
            f"{member}: the vendored copy ends at {retained[-1]} and upstream now starts "
            f"at {years[0]}; keeping the old years would leave a gap"
        )
    if retained[-1] > int(previous["exact_through"]):
        # Not a case the data has produced; refuse rather than carry forward
        # years nobody verified as though they had been.
        raise NormalizationError(
            f"{member}: the vendored copy's {retained[-1]} was not verified exact"
        )

    # Each retained year keeps the source it was read from: a year carried
    # forward before keeps its original provenance, the rest came from the
    # vendored copy's own source.
    sources: dict[str, dict[str, Any]] = {}
    for year in retained:
        source = next(
            (r["source"] for r in previous.get("retained", []) if year in r["years"]),
            previous["source"],
        )
        entry = sources.setdefault(source["member_sha256"], {"source": source, "years": []})
        entry["years"].append(year)

    data, holidays = payload["data"], payload["holidays"]
    fall_back_hour = payload["fall_back_hour"]
    for year in retained:
        key = str(year)
        data[key] = previous["data"][key]
        if key in previous["holidays"]:
            holidays[key] = previous["holidays"][key]
        # A copy vendored before the label was recorded predates the files
        # that changed it, and verified exact under the HS2 reading.
        fall_back_hour[key] = previous.get("fall_back_hour", {}).get(key, 2)
    # No re-sorting needed: the payload is written with sort_keys.
    payload["years"] = retained + years
    payload["retained"] = list(sources.values())
    return retained


def _is_complete(matrix: list[list[list[float | None]]] | None) -> bool:
    if matrix is None:
        return False
    return all(value is not None for month in matrix for day in month for value in day)


def _require_complete(matrix: list[list[list[float | None]]]) -> list[list[list[float]]]:
    """Narrow a verified-complete matrix, dropping the Optional."""
    return [[[v for v in day if v is not None] for day in month] for month in matrix]


def fetch_zip(cache: Path, *, refresh: bool = False) -> Path:
    """Download the upstream archive, caching it outside the repo tree."""
    import httpx

    from .fetch import _age_days

    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and not refresh:
        # Said out loud, with its age: a cached archive from before PG&E's last
        # publication regenerates the old data and reports it current. That is
        # how the November 2026 vintages first looked from a working copy.
        print(f"using cached archive {cache} ({_age_days(cache)}), --refresh to re-download")
        return cache
    print(f"downloading {SOURCE_URL}")
    # Into a sibling and renamed once complete, so an interrupted download
    # never leaves a truncated archive that the next run would take as cached.
    partial = cache.with_name(cache.name + ".part")
    with httpx.stream("GET", SOURCE_URL, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    partial.replace(cache)
    return cache


def vintage_slug(rate_name: str) -> str:
    return rate_name.lower()


_ONE_DAY = timedelta(days=1)


def intersect_holidays(by_vintage: dict[str, set[date]]) -> dict[int, list[date]]:
    """Per-year intersection across the vintages that cover that year.

    Every vintage agrees on the canonical eight holidays through 2036. Later
    years pick up per-file artifacts, and intersecting removes them -- but only
    where two vintages actually disagree. 2044 is covered by NBT00/25/26 and
    2045 by NBT26 alone, and each of them duplicates Memorial Day, Independence
    Day and Labor Day onto the following day, so the intersection preserved the
    duplicate and both years came out with eleven holidays instead of eight.
    E-TOU-D prices its 5-8pm peak as off-peak on a holiday, so those six
    weekday evenings were mispriced.

    A holiday immediately following another is that artifact: PG&E's observed
    rules move a holiday to the preceding Friday or the following Monday, and
    never produce two consecutive dates. Where a year legitimately carries a
    ninth entry -- 2027 and 2032 each observe both Christmas Eve and New
    Year's Eve -- the dates are far apart and survive.
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
        common -= {day for day in common if day - _ONE_DAY in common}
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
        "# These holidays set the EXPORT day type, and also E-TOU-D's peak, which",
        "# applies on weekdays only and prices a holiday as off-peak. E-ELEC,",
        "# E-TOU-C and EV2-A make no weekday/weekend/holiday distinction.",
        "#",
        "# This is the INTERSECTION across the vintage files covering each year,",
        "# less any date that immediately follows another. They agree through 2036;",
        "# beyond that NBT25/26/00 duplicate some holidays onto the following day.",
        "# Intersecting drops those wherever two vintages disagree, but 2044 is",
        "# covered by three files and 2045 by one, all carrying the duplicate, so",
        "# both years survived it at eleven holidays until the adjacency rule was",
        "# added. PG&E's observed-date rules never produce consecutive dates.",
        "# Export",
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
        "--refresh",
        action="store_true",
        help="with --download, fetch the archive even when a cached copy exists",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE / "PGE-Solar-Billing-Plan-Export-Rates.zip",
    )
    args = parser.parse_args(argv)

    archive = fetch_zip(args.cache, refresh=args.refresh) if args.download else args.zip
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
            payload["source"] = {
                "url": SOURCE_URL,
                "member": member,
                "member_sha256": member_sha,
                "archive_sha256": archive_sha,
            }
            slug = vintage_slug(rate_name)
            target = EXPORT_DIR / f"{slug}.json.gz"
            previous = json.loads(gzip.decompress(target.read_bytes())) if target.exists() else None
            retained = retain_dropped_years(payload, previous, member)
            if retained:
                print(f"{member}: kept {retained[0]}-{retained[-1]} from the vendored copy")
            holidays_by_vintage[rate_name] = holidays | {
                date.fromisoformat(day)
                for year in retained
                for day in payload["holidays"].get(str(year), [])
            }
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
            if retained:
                manifest_vintages[rate_name]["retained_years"] = retained

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
