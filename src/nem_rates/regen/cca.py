"""Regenerating a CCA's generation rate card.

A Community Choice Aggregator supplies generation only; delivery stays with the
utility. So its rate card is one number per schedule, season and time-of-use
period, and it has to be vendored separately from the utility's tariff even when
the two happen to agree.

They do agree today. Every MCE value currently equals PG&E's generation
component exactly -- MCE prices at parity. That is a fact about this moment, not
a rule, so the card is extracted and stored on its own and the parity is
*reported* rather than required. Deriving MCE's rates from PG&E's would be
smaller code and would silently produce wrong prices the day MCE moves.

The cards are laid out as a schedule header, then a season line, then one line
per period:

    ELEC - Residential Time-of-Use for Qualified Electric Technologies
    Summer - Service June 1 through September 30
    Peak $0.301/kWh 4 P.M. to 9 P.M. every day
    Part-Peak $0.199/kWh 3 P.M. to 4 P.M. and 9 P.M. to 12 A.M. every day
    Off Peak $0.152/kWh All other hours

Schedules a card lists but the library does not vendor -- MCE publishes ETOUD,
E1 and a block of closed schedules -- are skipped rather than guessed at, and
named in the run's output so a newly relevant one is noticed.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .emit import DATA_DIR, Result, fmt, write_or_check
from .providers import Cca
from .sheets import ExtractionError, Page, cells, read_pages

#: "Peak", "Part-Peak", "Off Peak" -> the period key. Spellings vary by card:
#: MCE writes "Off Peak", PG&E "OFF-PEAK".
PERIOD_KEYS = {
    "peak": "peak",
    "partpeak": "part_peak",
    "offpeak": "off_peak",
}

#: A schedule header: a bare code, a dash, then prose. The dash is written as an
#: escape because cards use both the hyphen and the en dash, and the two are
#: indistinguishable on screen.
SCHEDULE_HEADER = re.compile("^([A-Z][A-Z0-9\\-]{1,12})\\s*[-\u2013]\\s*[A-Za-z]")
#: A period line: the period name, then the rate, then when it applies.
PERIOD_LINE = re.compile(r"^(Peak|Part[-\s]?Peak|Off[-\s]?Peak)\b(.*)$", re.I)
#: A flat schedule with no periods at all, e.g. "E1, EM, ES - Basic $0.149/kWh".
SEASON_LINE = re.compile(r"^(Summer|Winter)\b", re.I)

#: Rates outside this band are a parse error, not a price. MCE's have ranged
#: from about 6 to 31 cents; an order of magnitude either side means the label
#: and the figure came from different columns.
PLAUSIBLE = (0.01, 1.0)


def _period_key(raw: str) -> str | None:
    return PERIOD_KEYS.get(re.sub(r"[^a-z]", "", raw.lower()))


def extract_generation(
    pages: list[Page], aliases: dict[str, str]
) -> tuple[dict[str, dict[str, dict[str, float]]], list[str]]:
    """``({slug: {season: {period: rate}}}, skipped schedule codes)``.

    Only schedules named in ``aliases`` are kept; the rest are returned so the
    caller can say what was passed over.
    """
    found: dict[str, dict[str, dict[str, float]]] = {}
    skipped: list[str] = []
    schedule: str | None = None
    season: str | None = None
    closed = False

    for page in pages:
        for raw in page.text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.upper().startswith("CLOSED"):
                # Everything after this heading is closed to new enrolment and
                # priced for legacy customers only.
                closed = True
                continue
            if header := SCHEDULE_HEADER.match(line):
                code = header.group(1).upper().rstrip("-")
                schedule, season = aliases.get(code), None
                if schedule is None and not closed and code not in skipped:
                    skipped.append(code)
                continue
            if season_match := SEASON_LINE.match(line):
                season = season_match.group(1).lower()
                continue
            period_match = PERIOD_LINE.match(line)
            if not (period_match and schedule and season and not closed):
                continue
            period = _period_key(period_match.group(1))
            values = cells(period_match.group(2))
            if period is None or not values:
                continue
            rate = values[0]
            if not PLAUSIBLE[0] <= rate <= PLAUSIBLE[1]:
                raise ExtractionError(
                    f"{schedule} {season} {period}: {rate} is outside the plausible "
                    f"range {PLAUSIBLE}; the label and the figure likely disagree"
                )
            found.setdefault(schedule, {}).setdefault(season, {})[period] = rate

    if not found:
        raise ExtractionError("no schedule rate tables found in the rate card")
    return found, skipped


def verify(generation: dict[str, dict[str, dict[str, float]]]) -> list[str]:
    """Both seasons for every schedule, and a period set that agrees between them."""
    problems: list[str] = []
    for slug, seasons in sorted(generation.items()):
        missing = {"summer", "winter"} - set(seasons)
        if missing:
            problems.append(f"{slug}: no {', '.join(sorted(missing))} rates")
            continue
        if set(seasons["summer"]) != set(seasons["winter"]):
            problems.append(
                f"{slug}: summer has {sorted(seasons['summer'])} but winter has "
                f"{sorted(seasons['winter'])}; a period was misread"
            )
    return problems


def parity_note(generation: dict[str, dict[str, dict[str, float]]], utility_key: str) -> list[str]:
    """Report where the card matches the utility's own generation component.

    Not a check. Parity is what happens to be true today, and the whole reason
    this data is vendored separately is so the day it stops being true is
    visible rather than silent.
    """
    from ..tariff.retail import load_snapshot
    from .providers import utility as get_utility

    notes: list[str] = []
    names = get_utility(utility_key).schedule_names
    for slug, seasons in sorted(generation.items()):
        try:
            snapshot = load_snapshot(utility_key.upper(), names[slug], date.today())
        except Exception:
            continue
        same = differs = 0
        for season, periods in seasons.items():
            for period, rate in periods.items():
                theirs = snapshot.raw["energy"].get(season, {}).get(period, {}).get("generation")
                if theirs is None:
                    continue
                same, differs = (
                    (same + 1, differs) if abs(theirs - rate) < 5e-6 else (same, differs + 1)
                )
        if differs:
            notes.append(
                f"{slug}: {differs} of {same + differs} rates now DIFFER from {utility_key.upper()}"
            )
        elif same:
            notes.append(f"{slug}: all {same} rates still at parity with {utility_key.upper()}")
    return notes


def render(
    provider: Cca,
    generation: dict[str, dict[str, dict[str, float]]],
    effective: date,
    previous: dict[str, object],
    names: dict[str, str],
) -> str:
    lines = [
        f"# {provider.name} ({provider.key.upper()}) residential generation rates.",
        "#",
        f"# Source: {provider.rate_card.url}",
    ]
    if provider.tariff_url:
        lines.append(f"# Solar Billing Plan tariff: {provider.tariff_url}")
    lines += [
        "#",
        "# GENERATED by `nem-rates regen cca` -- do not hand-edit the rate tables.",
        "#",
        "# A CCA supplies generation only; delivery stays with the utility, so these",
        "# are generation rates alone and pair with that utility's tariff sheet.",
        "#",
        "# These may currently equal the utility's own generation component exactly.",
        "# That is parity today, not an alias: they are vendored separately so a",
        "# divergence shows up rather than being silently inherited. Regeneration",
        "# reports the parity status of every rate it writes.",
        "",
        "schema = 1",
        f'provider = "{provider.key.upper()}"',
        f'name = "{provider.name}"',
        f'utility = "{provider.utility.upper()}"',
        f"schedules = {fmt([names[s] for s in sorted(generation)])}",
        f'effective = "{effective.isoformat()}"',
        f'currency = "{previous.get("currency", "USD/kWh")}"',
        f'source_url = "{provider.rate_card.url}"',
    ]
    for slug in sorted(generation):
        for season in ("summer", "winter"):
            periods = generation[slug].get(season, {})
            if not periods:
                continue
            lines.append(f"\n[generation.{slug}.{season}]")
            for period in ("peak", "part_peak", "off_peak"):
                if period in periods:
                    lines.append(f"{period} = {fmt(periods[period])}")
    return "\n".join(lines) + "\n"


def regenerate(provider: Cca, pdf: Path, *, check: bool) -> Result:
    import tomllib

    pages = read_pages(pdf)
    generation, skipped = extract_generation(pages, provider.schedule_aliases)
    problems = verify(generation)

    messages: list[str] = []
    if skipped:
        messages.append(f"skipped {len(skipped)} unvendored schedule(s): {', '.join(skipped)}")
    if problems:
        return Result(
            provider.key,
            changed=False,
            failed=True,
            messages=(*messages, "REFUSING to write:", *[f"    {p}" for p in problems]),
        )
    counted = sum(len(p) for s in generation.values() for p in s.values())
    messages.append(f"{counted} rates across {len(generation)} schedule(s)")
    messages += parity_note(generation, provider.utility)

    target = DATA_DIR / "cca" / f"{provider.key}.toml"
    previous = tomllib.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    effective = next(
        (p.effective for p in pages if p.effective),
        date.fromisoformat(str(previous.get("effective", date.today()))),
    )
    from .providers import utility as get_utility

    body = render(
        provider, generation, effective, previous, get_utility(provider.utility).schedule_names
    )
    return write_or_check(provider.key, target, body, check=check, messages=messages)
