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

**A card with no text layer is still watched.** MCE's April 2026 card was a
print-to-PDF export whose figures are glyph ids with no characters behind them,
so nothing could parse it and the values were read from the rendered page
instead. (Its November reissue has text again, and is extracted.) That stops the
*extraction* being automatic; it does not stop the *detection*, which was always
the more valuable half. The vendored file records a checksum of
the document it was read from, so a scheduled run downloads the card, compares
bytes, and says whether a human needs to go and look. Silence then means the
publisher has not moved, rather than meaning nobody checked.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tariffkit.errors import ConfigError, DataError

from .emit import DATA_DIR, Result, fmt, write_or_check
from .providers import Cca, utility
from .sheets import ExtractionError, Page, cells, read_pages

#: "Peak", "Part-Peak", "Off Peak" -> the period key. Spellings vary by card:
#: MCE writes "Off Peak", PG&E "OFF-PEAK".
PERIOD_KEYS = {
    "peak": "peak",
    "partpeak": "part_peak",
    "offpeak": "off_peak",
}

#: A schedule header: the code, optionally more codes sharing the row, a dash,
#: then prose. The dash is written as an escape because cards use both the
#: hyphen and the en dash, and the two are indistinguishable on screen.
#:
#: The trailing code list is not decoration. From the December 2023 print
#: onward MCE writes "ETOUC, EMTOUC - Default Residential Time-of-Use", which
#: a pattern anchored on a single code ahead of the dash does not match -- so
#: E-TOU-C was dropped from the card entirely, and because a line that matches
#: nothing is not a schedule it was not recorded in `skipped` either. The
#: result was a clean-looking 12-rate file with a whole schedule missing.
SCHEDULE_HEADER = re.compile(
    "^([A-Z][A-Z0-9\\-]{1,12})(?:\\s*,\\s*[A-Z][A-Z0-9\\-]{1,12})*\\s*[-\u2013]\\s*[A-Za-z]"
)
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


#: "MCE Deep Green Premium / All Usage* as above plus $0.0125/kWh"
DEEP_GREEN = re.compile(r"Deep Green Premium.{0,120}?plus\s*\$([0-9.]+)\s*/?\s*kWh", re.I | re.S)


def extract_options(pages: list[Page]) -> dict[str, float]:
    """Service options and what each adds per kWh.

    Read rather than carried forward: the premium moves. The 2023 card charges
    $0.01/kWh for Deep Green where the 2026 one charges $0.0125, so inheriting
    it across vintages would misprice whichever end you inherited from.
    """
    text = " ".join(page.text for page in pages)
    match = DEEP_GREEN.search(text)
    if not match:
        return {}
    return {"light_green": 0.0, "deep_green": float(match.group(1))}


#: "MCE Cost Relief Credit (-$0.03725)/kWh *Runs from November 1, 2026 through
#: March 31, 2027". The April 2026 print had no start, only "Runs through
#: December 31, 2026", so the start is optional.
_SPELLED_DATE = r"[A-Z][a-z]+ \d{1,2}, \d{4}"
COST_RELIEF = re.compile(
    r"Cost Relief Credit[\s(]*-?\s*\$\s*([0-9.]+)\s*\)?\s*/\s*kWh.{0,120}?"
    rf"Runs\s+(?:from\s+({_SPELLED_DATE})\s+)?through\s+({_SPELLED_DATE})",
    re.I | re.S,
)


def _spelled(text: str) -> str:
    return datetime.strptime(text, "%B %d, %Y").date().isoformat()


def extract_cost_relief_credit(pages: list[Page]) -> dict[str, Any] | None:
    """The credit window the card states, or None when it states none.

    Read rather than carried forward, for the same reason as the Deep Green
    premium: in November 2026 MCE reissued its April card with a credit six
    times the size from 2026-11-01, under the same "Rates effective April 1,
    2026" heading and with every rate unchanged. Carrying the old credit
    forward regenerates a file that passes every test and prices every
    November kWh with the wrong one.

    A card that names the credit in a form this does not read raises rather
    than returning None: "no credit" and "could not read the credit" must not
    look the same.
    """
    text = " ".join(page.text for page in pages)
    if "cost relief credit" not in text.lower():
        return None
    match = COST_RELIEF.search(text)
    if not match:
        start = text.lower().index("cost relief credit")
        raise ExtractionError(
            "the card names a Cost Relief Credit but not in a form this reads: "
            f"{' '.join(text[start : start + 160].split())!r}"
        )
    rate, start_text, end_text = match.groups()
    window: dict[str, Any] = {"rate": -abs(float(rate))}
    try:
        if start_text:
            window["from"] = _spelled(start_text)
        window["through"] = _spelled(end_text)
    except ValueError as exc:
        raise ExtractionError(f"cost relief credit window is not a real date: {exc}") from exc
    return window


def _day(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def merge_credit_windows(
    recorded: object, stated: dict[str, Any] | None, effective: date, today: date
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``(windows, messages, problems)`` for the vintage being written.

    A card states only the credit running now, so the history lives in the
    vendored file: windows that ended before the stated one began are kept, a
    window still open when the stated one begins is ended the day before (the
    card lists one credit, so the new one replaced it rather than stacking),
    and one starting on or after it is superseded. Windows that ended before
    this vintage took force are dropped, since they can never apply to it.

    A card that states no credit while the file has one still running is a
    problem rather than a carry-forward: either the credit ended early or the
    card was misread, and only a person can say which.
    """
    if isinstance(recorded, dict):
        recorded = [recorded]
    windows = [
        dict(w)
        for w in (recorded if isinstance(recorded, list) else [])
        if (end := _day(w.get("through"))) is None or end >= effective
    ]
    if stated is None:
        running = [w for w in windows if (end := _day(w.get("through"))) is None or end >= today]
        if running:
            spans = ", ".join(
                f"{fmt(w['rate'])} through {w.get('through', 'open')}" for w in running
            )
            return (
                windows,
                [],
                [
                    f"the card states no cost relief credit, but the vendored file has "
                    f"one still running ({spans}); confirm when it ended and record that"
                ],
            )
        return windows, [], []
    if stated in windows:
        return windows, [], []

    start = _day(stated.get("from"))
    kept: list[dict[str, Any]] = []
    messages: list[str] = []
    for window in windows:
        window_start = _day(window.get("from"))
        if start is None or (window_start is not None and window_start >= start):
            messages.append(
                f"cost relief credit {fmt(window['rate'])} through "
                f"{window.get('through', 'open')} superseded by {fmt(stated['rate'])}"
            )
            continue
        end = _day(window.get("through"))
        if end is None or end >= start:
            window["through"] = (start - timedelta(days=1)).isoformat()
            messages.append(
                f"cost relief credit {fmt(window['rate'])} now ends {window['through']}: "
                f"the card replaces it with {fmt(stated['rate'])} from {stated['from']}"
            )
        kept.append(window)
    kept.append(stated)
    messages.append(
        f"cost relief credit {fmt(stated['rate'])} "
        f"{'from ' + stated['from'] + ' ' if 'from' in stated else ''}"
        f"through {stated['through']}"
    )
    return kept, messages, []


def preamble(existing: str, title: str) -> tuple[list[str], bool]:
    """Hand-written notes above a vintage's generated header, and whether found.

    Regeneration rewrites the file, and the notes are where a vintage records
    what the card alone does not prove -- which rates a statement confirmed,
    how the export basis was measured. They sit above the generated title and
    are kept verbatim. A file with no generated title was written by hand, so
    its whole leading comment block is kept instead, and the caller says so.
    """
    lines = existing.splitlines()
    if title in lines:
        return lines[: lines.index(title)], True
    block: list[str] = []
    for line in lines:
        if line.strip() and not line.startswith("#"):
            break
        block.append(line)
    while block and not block[-1].strip():
        block.pop()
    return ([*block, "#"] if block else []), False


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
    from tariffkit.tariff.retail import load_snapshot

    notes: list[str] = []
    utility_provider = utility(utility_key)
    names = utility_provider.schedule_names
    for slug, seasons in sorted(generation.items()):
        try:
            snapshot = load_snapshot(utility_provider.identifier, names[slug], date.today())
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
                f"{slug}: {differs} of {same + differs} rates now DIFFER from "
                f"{utility_provider.short_name}"
            )
        elif same:
            notes.append(
                f"{slug}: all {same} rates still at parity with {utility_provider.short_name}"
            )
    return notes


def title(provider: Cca) -> str:
    """The generated header's first line; everything above it is hand-written."""
    return f"# {provider.name} ({provider.key.upper()}) residential generation rates."


def render(
    provider: Cca,
    generation: dict[str, dict[str, dict[str, float]]],
    effective: date,
    previous: dict[str, object],
    names: dict[str, str],
    digest: str = "",
    source_url: str = "",
    options: dict[str, float] | None = None,
    credit_windows: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
    read_on: str = "",
) -> str:
    lines = [
        *(notes or []),
        title(provider),
        "#",
        f"# Source: {source_url or provider.rate_card.url}",
    ]
    if provider.tariff_url:
        lines.append(f"# Solar Billing Plan tariff: {provider.tariff_url}")
    lines += [
        "#",
        "# GENERATED by `python -m tools.regen cca` -- do not hand-edit the rate tables.",
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
        f'utility = "{utility(provider.utility).identifier}"',
        f"schedules = {fmt([names[s] for s in sorted(generation)])}",
        f'effective = "{effective.isoformat()}"',
        f'currency = "{previous.get("currency", "USD/kWh")}"',
        # The URL the vendored digest actually came from, not whichever card
        # the provider points at today. Hardcoding the current URL stamped
        # every historical vintage regenerated from a local --pdf with a
        # provenance line that does not hash to its own source_sha256.
        f'source_url = "{source_url or provider.rate_card.url}"',
        "",
        "# Checksum of the document these values were read from. A scheduled check",
        "# compares it so a republished card is noticed even when it cannot be",
        "# parsed -- detection does not need a text layer, only bytes.",
        f'source_sha256 = "{digest or previous.get("source_sha256", "")}"',
        f'source_read_on = "{read_on or previous.get("source_read_on", date.today().isoformat())}"',
    ]
    # The service options and the cost relief credit are read off the card; the
    # export terms come from the CCA's tariff rather than this table, so they are
    # carried forward. Regenerating the rates must not silently discard them.
    options = options or {}
    if options:
        lines.append("\n[options]")
        for key, value in options.items():
            lines.append(f"{key} = {fmt(value)}")
    for window in credit_windows or []:
        lines.append("\n[[cost_relief_credit]]")
        for key in ("rate", "from", "through"):
            if key in window:
                lines.append(f"{key} = {fmt(window[key])}")
    export = previous.get("export")
    if isinstance(export, dict):
        lines.append("\n[export]")
        for key, value in export.items():
            lines.append(f"{key} = {fmt(value)}")

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


def regenerate(provider: Cca, pdf: Path, *, check: bool, source_url: str = "") -> Result:

    directory = DATA_DIR / "cca" / provider.key
    existing = sorted(directory.glob("*.toml")) if directory.is_dir() else []
    vintages = [tomllib.loads(e.read_text(encoding="utf-8")) for e in existing]
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()

    try:
        pages = read_pages(pdf)
        generation, skipped = extract_generation(pages, provider.schedule_aliases)
    except ExtractionError as exc:
        newest_raw = max(vintages, key=lambda v: str(v["effective"])) if vintages else {}
        return _watch_by_checksum(provider, digest, newest_raw, exc)
    # Outside the fallback above: a card whose rates parse but whose credit does
    # not is a reader that needs fixing, not a card that can only be watched.
    try:
        stated_credit = extract_cost_relief_credit(pages)
    except ExtractionError as exc:
        return Result(provider.key, changed=False, failed=True, messages=(str(exc),))
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

    # The card states when it took force; fall back to the newest vintage on
    # disk only when it does not.
    newest = max((str(v["effective"]) for v in vintages), default=date.today().isoformat())
    effective = next((p.effective for p in pages if p.effective), None)
    if effective is None:
        effective = date.fromisoformat(newest)
        # Said out loud: a repriced card that stopped dating itself would
        # otherwise overwrite the newest vintage with no sign of having guessed.
        messages.append(f"the card states no effective date; filed under {newest}, the newest")
    # Keep a vintage's own non-rate terms when refreshing it; otherwise carry
    # them forward from an earlier card. Inheriting from a later card gave the
    # 2023 vintage a cost relief credit that did not exist until 2026 -- the card
    # does not mention one and the December 2025 statement does not charge one,
    # so it was $5.97 of invented credit.
    target = directory / f"{effective.isoformat()}.toml"
    earlier = [v for v in vintages if str(v.get("effective", "")) < effective.isoformat()]
    if target.exists():
        # A re-read of an existing card cannot reconstruct its non-rate terms;
        # keep those terms from the vintage being refreshed.
        previous = tomllib.loads(target.read_text(encoding="utf-8"))
    else:
        previous = max(earlier, key=lambda v: str(v["effective"])) if earlier else {}
    if vintages and not target.exists() and not earlier:
        messages.append(
            "oldest vintage: no earlier card to carry terms from, so the cost relief "
            "credit and export sections are omitted. Add what this card states."
        )

    today = date.today()
    windows, credit_messages, credit_problems = merge_credit_windows(
        previous.get("cost_relief_credit"), stated_credit, effective, today
    )
    messages += credit_messages
    if credit_problems:
        return Result(
            provider.key,
            changed=False,
            failed=True,
            messages=(*messages, "REFUSING to write:", *[f"    {p}" for p in credit_problems]),
        )

    notes: list[str] = []
    if target.exists():
        notes, generated = preamble(target.read_text(encoding="utf-8"), title(provider))
        if notes and not generated:
            messages.append(
                f"{target.name} was written by hand; its leading comments are kept as "
                f"notes above the generated header, but comments inside its tables are not"
            )

    from .providers import utility as get_utility

    # Provenance follows the document this actually read, not the card the
    # provider points at today. A historical `--pdf` has no URL of its own, so
    # the vintage it rewrites keeps the one already recorded against that same
    # digest; only a genuinely new document falls back to the live URL.
    if not source_url and target.exists():
        recorded = tomllib.loads(target.read_text(encoding="utf-8"))
        if str(recorded.get("source_sha256", "")) == digest:
            source_url = str(recorded.get("source_url", ""))

    body = render(
        provider,
        generation,
        effective,
        previous,
        get_utility(provider.utility).schedule_names,
        digest=digest,
        source_url=source_url,
        options=extract_options(pages),
        credit_windows=windows,
        notes=notes,
        # A document the file has not recorded was read today; keeping the old
        # date dated a November card's reading to August.
        read_on=("" if str(previous.get("source_sha256", "")) == digest else today.isoformat()),
    )
    # Dated from the card's own statement of when it took force, so a historical
    # card lands beside the current one rather than overwriting it.
    return write_or_check(
        f"{provider.key} {effective}",
        target,
        body,
        check=check,
        verify=lambda text: verify_against_library(provider, text),
        messages=messages,
    )


def verify_against_library(provider: Cca, text: str) -> list[str]:
    """Problems the library would have reading ``text``, before it is written.

    The card is put through the loader's own validation, then every rate is
    read back and every credit window priced at both ends. A card that printed
    "from November 1, 2026 through March 31, 2026" used to extract and merge
    cleanly, then be written as a file the loader refuses -- taking down MCE
    pricing rather than the regeneration that caused it.
    """
    from tariffkit.cca import parse_rate_card

    try:
        card = parse_rate_card(provider.key.upper(), tomllib.loads(text))
    except (DataError, tomllib.TOMLDecodeError) as exc:
        return [str(exc)]
    problems: list[str] = []
    for slug, seasons in card.raw.get("generation", {}).items():
        for season, periods in seasons.items():
            for period, rate in periods.items():
                try:
                    read = card.generation(slug, season, period)
                except (ConfigError, DataError) as exc:
                    # ConfigError is a missing [options] table: the card did
                    # not state Deep Green, so even Light Green cannot price.
                    problems.append(str(exc))
                    continue
                if abs(read - float(rate)) > 1e-9:
                    problems.append(f"{slug} {season} {period}: wrote {rate}, read back {read}")
    for window in card.raw.get("cost_relief_credit", []):
        for key in ("from", "through"):
            if key not in window:
                continue
            on = date.fromisoformat(str(window[key]))
            read = card.cost_relief_credit(on)
            if abs(read - float(window["rate"])) > 1e-9:
                problems.append(
                    f"cost relief credit on {on}: wrote {window['rate']}, read back {read}"
                )
    return problems


def _watch_by_checksum(
    provider: Cca, digest: str, previous: dict[str, object], why: ExtractionError
) -> Result:
    """Report whether an unparseable card has changed since it was last read.

    The card cannot be extracted, so this cannot rebuild the file. It can still
    answer the question a scheduled check exists to answer -- has the publisher
    moved? -- by comparing bytes against the document the vendored values were
    read from.
    """
    known = str(previous.get("source_sha256", ""))
    read_on = str(previous.get("source_read_on", "an unrecorded date"))
    if not known:
        return Result(
            provider.key,
            changed=False,
            failed=True,
            messages=(
                f"cannot parse this card and {provider.key}.toml records no "
                f"source_sha256 to compare against, so a change cannot be detected. "
                f"Read the rendered page, update the values, and record the checksum "
                f"{digest}.",
                str(why),
            ),
        )
    if known == digest:
        return Result(
            provider.key,
            changed=False,
            failed=False,
            messages=(
                f"source unchanged since it was read on {read_on} (sha256 {digest[:12]}); "
                f"its values cannot be re-parsed, but the publisher has not moved",
            ),
        )
    return Result(
        provider.key,
        changed=True,
        failed=False,
        messages=(
            f"SOURCE CHANGED since {read_on}: sha256 was {known[:12]}, now {digest[:12]}.",
            f"This card has no text layer, so re-read it from the rendered page and "
            f'update {provider.key}.toml, then record source_sha256 = "{digest}".',
            f"Document: {provider.rate_card.url}",
        ),
    )
