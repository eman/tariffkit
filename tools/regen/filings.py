"""Finding the filing that adopted a rate vintage.

``regen tariff --advice-letter`` rebuilds a superseded vintage once you know
which filing to ask for. Knowing that is the hard part: a utility numbers its
advice letters sequentially across everything it files, so the one that changed
residential rates last October sits among hundreds about interconnection queues
and depreciation studies, and nothing in the number says which is which.

So this indexes them. For a range of numbers it asks what size each filing is --
a consolidated rate change is tens of megabytes where a routine filing is one --
then reads the plausible ones and records which schedules each revises and from
what date. The index is cached, because the reading is the expensive half and
the answer does not change once a filing is issued.

    python -m tools.regen tariff --for-date 2025-12-15

then resolves a date to the filing in force on it, and rebuilds that vintage.

The size filter is a heuristic and is treated as one: it decides what to *read*,
never what to trust. Everything the index records comes from the sheets' own
headers, and a filing that turns out to revise nothing relevant is recorded as
such rather than skipped, so the next run does not fetch it again.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .providers import USER_AGENT, Utility
from .sheets import SHEET_HEADER, ExtractionError, parse_effective, read_pages

#: Below this a filing is routine -- a page or two of tariff text. A
#: consolidated rate change carries every schedule the utility revised and runs
#: to tens of megabytes. Used only to decide what is worth downloading.
MIN_INTERESTING_BYTES = 5_000_000

#: How many numbers to probe at once. Politeness, not throughput.
PROBE_WORKERS = 8


@dataclass(frozen=True, slots=True)
class Filing:
    """One advice letter, and what it was found to revise."""

    number: str
    size: int
    #: Sheet-header name -> the effective date those sheets carry.
    schedules: dict[str, str]

    def effective_for(self, sheet_name: str) -> date | None:
        stamp = self.schedules.get(sheet_name.upper())
        return date.fromisoformat(stamp) if stamp else None


def index_path(cache: Path, utility: str) -> Path:
    return cache / f"{utility}-filings.json"


def load_index(cache: Path, utility: str) -> dict[str, Filing]:
    path = index_path(cache, utility)
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: Filing(k, v["size"], v["schedules"]) for k, v in raw.items()}


def save_index(cache: Path, utility: str, found: dict[str, Filing]) -> None:
    path = index_path(cache, utility)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {k: {"size": v.size, "schedules": v.schedules} for k, v in sorted(found.items())},
            indent=1,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def probe_sizes(provider: Utility, numbers: list[int]) -> dict[int, int]:
    """How large each filing is, by HEAD request. Zero when absent."""
    import httpx

    def one(number: int) -> tuple[int, int]:
        url = provider.advice_letter_url.format(number=f"{number}-E")
        try:
            with httpx.Client(
                follow_redirects=True, timeout=30, headers={"User-Agent": USER_AGENT}
            ) as client:
                response = client.head(url)
            if response.status_code != 200:
                return number, 0
            return number, int(response.headers.get("content-length", 0))
        except Exception:
            return number, 0

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        return dict(pool.map(one, numbers))


def read_filing(provider: Utility, number: str, cache: Path) -> Filing:
    """Download one filing and record which schedules it revises, and from when."""
    from .fetch import fetch
    from .providers import Source

    path = fetch(Source(provider.advice_letter_url.format(number=number)), cache / f"{number}.pdf")
    schedules: dict[str, str] = {}
    for page in read_pages(path):
        match = SHEET_HEADER.search(page.text)
        if not match:
            continue
        name = match.group(1).upper()
        when = page.effective or parse_effective(page.text)
        if when and name not in schedules:
            schedules[name] = when.isoformat()
    return Filing(number, path.stat().st_size, schedules)


def build_index(
    provider: Utility,
    lo: int,
    hi: int,
    cache: Path,
    *,
    refresh: bool = False,
    report: object = print,
) -> dict[str, Filing]:
    """Index every filing in ``[lo, hi]`` worth reading, reusing what is cached."""
    if not provider.advice_letter_url:
        raise ExtractionError(f"{provider.key} does not publish advice letters at a known address")

    found = {} if refresh else load_index(cache, provider.key)
    sizes = probe_sizes(provider, [n for n in range(lo, hi + 1) if f"{n}-E" not in found])
    worth = sorted(n for n, size in sizes.items() if size >= MIN_INTERESTING_BYTES)
    if callable(report):
        report(
            f"    {len(sizes)} filings probed, {len(worth)} large enough to read, "
            f"{len(found)} already indexed"
        )
    for number in worth:
        entry = read_filing(provider, f"{number}-E", cache)
        found[entry.number] = entry
        if callable(report) and entry.schedules:
            report(
                f"    {entry.number}: {len(entry.schedules)} schedules, "
                f"effective {sorted(set(entry.schedules.values()))}"
            )
    # Record the ones deliberately not read, so a rerun does not re-probe them.
    for number, size in sizes.items():
        found.setdefault(f"{number}-E", Filing(f"{number}-E", size, {}))
    save_index(cache, provider.key, found)
    return found


def filing_for(
    provider: Utility, sheet_name: str, on: date, indexed: dict[str, Filing]
) -> Filing | None:
    """The indexed filing whose sheets for ``sheet_name`` were in force on ``on``.

    The latest one effective on or before the date -- the same rule the data
    layer uses to pick a vintage, applied to the filings that produced them.
    """
    candidates = [
        (when, entry)
        for entry in indexed.values()
        if (when := entry.effective_for(sheet_name)) and when <= on
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]
