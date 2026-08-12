"""Regenerate a vendored retail tariff snapshot from a utility's published sheet.

Retail rates change far more often than export rates -- three times in the first
half of 2026 -- and were hand-transcribed from the tariff PDF for most of that.
That made the *detection* of a change the weak point: nothing noticed a new
advice letter, so a stale snapshot surfaced only when a bill disagreed.

    nem-rates regen tariff
    nem-rates regen tariff --check
    nem-rates regen tariff --provider etouc --pdf /path/to/sheet.pdf

The tariff sheets turn out to be highly regular. Every rate line is a label
followed by one value per time-of-use period, negatives in parentheses and an
optional (I)/(R)/(N)/(L) change marker per cell:

    Summer Usage                        $0.26299  $0.16388  $0.11878
    Nuclear Decommissioning (all usage) ($0.00002) ($0.00002) ($0.00002)

**What this does not invent.** A sheet publishes rates, not structure. Season
boundaries, time-of-use period hours, which components a CCA customer drops, and
the franchise fee vintages (a different schedule entirely, E-FFS) are carried
forward from the previous snapshot rather than guessed. If a *structural* thing
changes -- a schedule gains a part-peak period, say -- that is still a human
edit, and it should be.

**How it knows it parsed correctly.** The same check that caught hand
transcription slips: the unbundled components must sum to the sheet's own
published totals, which live in a separate table on a different page. Nothing is
written unless every season/period cell reconciles to the fifth decimal. That
makes a silent mis-parse essentially impossible -- it would have to be wrong in
two tables in exactly compensating ways.

**Sheets revise independently.** A tariff book is a compilation, and its sheets
carry their own advice letters and effective dates: E-ELEC's totals were revised
by 7921-E effective 2026-06-01 while the unbundled table it must reconcile
against was last touched by 7846-E effective 2026-03-01. The snapshot records
the latest effective date among the sheets actually used, and lists each one, so
provenance survives.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import franchise
from .emit import DATA_DIR, Result, fmt, write_or_check
from .providers import Utility
from .sheets import (
    SHEET_HEADER,
    TRAILING_CELLS,
    ExtractionError,
    Page,
    cells,
    clean_label,
    rate_lines,
    read_pages,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


#: Sheet label -> the key used in the snapshot. Footnote markers and the
#: "(all usage)" qualifier are stripped before matching, so only the component
#: name itself appears here.
ADDER_KEYS: dict[str, str] = {
    "transmission": "transmission",
    "transmission rate adjustments": "transmission_rate_adjustments",
    "reliability services": "reliability_services",
    "public purpose programs": "public_purpose_programs",
    "nuclear decommissioning": "nuclear_decommissioning",
    "competition transition charges": "competition_transition_charges",
    "energy cost recovery amount": "energy_cost_recovery",
    "wildfire fund charge": "wildfire_fund_charge",
    "new system generation charge": "new_system_generation",
    "wildfire hardening charge": "wildfire_hardening",
    "recovery bond charge": "recovery_bond_charge",
    "recovery bond credit": "recovery_bond_credit",
    "bundled power charge indifference adjustment": "bundled_pcia",
}

#: Structure the sheet does not publish, so it is carried forward rather than
#: regenerated. A change to any of these is a deliberate human edit.
CARRIED_FORWARD = (
    "seasons",
    "periods",
    "has_baseline",
    "discounts",
    "currency",
    "source_url",
)

#: Column header -> period key.
PERIOD_KEYS = {"PEAK": "peak", "PART-PEAK": "part_peak", "OFF-PEAK": "off_peak"}


@dataclass
class Extracted:
    """Everything read off the sheets, before merging with the previous snapshot."""

    periods: list[str]
    energy: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    adders: dict[str, float] = field(default_factory=dict)
    totals: dict[str, dict[str, float]] = field(default_factory=dict)
    base_services_charge: dict[str, float] = field(default_factory=dict)
    pcia_vintages: dict[int, float] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    provenance: list[tuple[int | None, str | None, date | None]] = field(default_factory=list)
    #: Effective date of the sheet the *rates* came from -- see pick_effective.
    rates_effective: date | None = None
    rates_advice: str | None = None


def _period_columns(text: str) -> list[str]:
    """Which time-of-use periods this schedule prices, from the column header."""
    for line in text.splitlines():
        if "Energy Rates by Component" in line:
            found = [PERIOD_KEYS[k] for k in ("PEAK", "PART-PEAK", "OFF-PEAK") if k in line]
            # "PEAK" is a substring of "PART-PEAK"/"OFF-PEAK", so recover order
            # from the header's own left-to-right sequence.
            order = re.findall(r"PART-PEAK|OFF-PEAK|PEAK", line)
            if order:
                return [PERIOD_KEYS[k] for k in order]
            return found
    raise ExtractionError("no 'Energy Rates by Component' column header found")


def extract_unbundled(sheet: Page) -> tuple[list[str], dict[str, Any], dict[str, float]]:
    """Energy components by season/period, plus the flat riders."""
    periods = _period_columns(sheet.text)
    energy: dict[str, dict[str, dict[str, float]]] = {}
    adders: dict[str, float] = {}
    section: str | None = None

    for raw in sheet.text.splitlines():
        line = raw.strip().rstrip("|").strip()
        if re.fullmatch(r"(Generation|Distribution)\*{0,2}\s*:", line, re.I):
            section = line.split("*")[0].strip(" :").lower()

    for label, values in rate_lines(sheet.text):
        low = label.lower()
        season = (
            "summer" if low.startswith("summer") else "winter" if low.startswith("winter") else None
        )
        if season:
            continue  # handled below, where section state is tracked
        key = ADDER_KEYS.get(clean_label(label))
        if key and values:
            # Riders are published once per period but have always been equal
            # across them, which is why [adders] stores one scalar. Taking the
            # first column silently would misprice the day that stops being
            # true, so disagreement is an error rather than a choice.
            if len({round(v, 9) for v in values}) > 1:
                raise ExtractionError(
                    f"{key} differs by period ({values}); [adders] holds one value "
                    f"per rider, so this schedule needs a period-aware adder table"
                )
            adders[key] = values[0]

    # Season rows need the Generation:/Distribution: header above them, so walk
    # the lines again carrying that state.
    section = None
    for raw in sheet.text.splitlines():
        line = raw.strip().rstrip("|").strip()
        if re.fullmatch(r"(Generation|Distribution)\*{0,2}\s*:", line, re.I):
            section = line.split("*")[0].strip(" :").lower()
            continue
        match = TRAILING_CELLS.search(line)
        if not match or section is None:
            continue
        label = line[: match.start()].strip().lower()
        season = (
            "summer"
            if label.startswith("summer")
            else "winter"
            if label.startswith("winter")
            else None
        )
        if season is None:
            continue
        values = cells(match.group(1))
        if len(values) != len(periods):
            raise ExtractionError(
                f"{section} {season}: got {len(values)} values for {len(periods)} periods"
            )
        for period, value in zip(periods, values, strict=True):
            energy.setdefault(season, {}).setdefault(period, {})[section] = value

    if not energy:
        raise ExtractionError("no Generation/Distribution rows found in the unbundled table")
    return periods, energy, adders


def extract_totals(sheets: list[Page], periods: list[str]) -> dict[str, dict[str, float]]:
    """The sheet's own published totals, used to verify the unbundled table.

    Two layouts in the wild. E-ELEC and EV2-A put the season on the rate line
    ("Summer Usage $... $... $..."); E-TOU-C puts the season on a line of its own
    and the figures on a "Total Usage" row beneath it. Both are handled by
    carrying the season forward until the next one appears.
    """
    totals: dict[str, dict[str, float]] = {}
    for sheet in sheets:
        if "Total Energy Rates" not in sheet.text:
            continue
        season: str | None = None
        for raw in sheet.text.splitlines():
            line = raw.strip().rstrip("|").strip()
            bare = line.lower().rstrip(":")
            if bare in ("summer", "winter"):
                season = bare
                continue
            match = TRAILING_CELLS.search(line)
            if not match:
                continue
            label = line[: match.start()].strip().lower()
            row_season = (
                "summer"
                if label.startswith("summer")
                else "winter"
                if label.startswith("winter")
                else season
                if label.startswith("total usage")
                else None
            )
            values = cells(match.group(1))
            if row_season and len(values) == len(periods) and row_season not in totals:
                totals[row_season] = dict(zip(periods, values, strict=True))
    if set(totals) != {"summer", "winter"}:
        raise ExtractionError(f"expected summer and winter totals, got {sorted(totals)}")
    return totals


def extract_base_services_charge(sheets: list[Page]) -> dict[str, float]:
    """Base services charge, $/customer/day, by income tier.

    Two published shapes, because the charge itself changed. When AB 205's charge
    began on 2026-03-01 it came with three income tiers; the flat per-meter
    charge that preceded it has one rate for everyone. A vintage with the flat
    form is recorded as three equal tiers rather than as a special case, so
    nothing downstream has to know which era it came from.
    """
    for sheet in sheets:
        found: dict[str, float] = {}
        if "Base Services Charge Rates ($" in sheet.text:
            for label, values in rate_lines(sheet.text):
                m = re.fullmatch(r"income tier (\d)", clean_label(label))
                if m and values:
                    found[f"tier_{m.group(1)}"] = values[0]
            if len(found) == 3:
                return found
        for label, values in rate_lines(sheet.text):
            if clean_label(label).startswith("base services charge") and values:
                flat = values[0]
                return {"tier_1": flat, "tier_2": flat, "tier_3": flat}
    # Absent, not unreadable: most schedules had no daily fixed charge before
    # AB 205's began on 2026-03-01, so an empty table is the right answer for an
    # earlier vintage and the snapshot simply omits the section.
    return {}


def extract_pcia(sheets: list[Page]) -> dict[int, float]:
    """Vintaged PCIA by year, from the sheet that publishes the vintage table.

    Read with its own row pattern rather than through :func:`rate_lines`, which
    wants the figures at end of line. The last row of this table runs straight
    into the next page's header -- "2026 Vintage ($0.01011) (N) (L)U 39Oakland,
    California" -- so a trailing match drops it, and dropping the newest vintage
    is the worst one to lose.
    """
    row = re.compile(r"\b(20\d{2})\s+Vintage\b[^0-9$(]*(\(?\$[0-9.]+\)?)", re.I)
    for sheet in sheets:
        if "Vintage Power Charge Indifference Adjustment" not in sheet.text:
            continue
        found: dict[int, float] = {}
        for year, money in row.findall(sheet.text):
            values = cells(money)
            if values:
                found[int(year)] = values[0]
        if found:
            return found
    return {}


def extract_baseline(sheets: list[Page], adders: dict[str, float]) -> dict[str, Any]:
    """Conservation Incentive Adjustment rates and the daily baseline quantities.

    The credit a bill prints is the *spread* between the two CIA rates, which is
    why it is derived here rather than read off the sheet: the sheet never prints
    it as one number.
    """
    within = over = None
    for sheet in sheets:
        for label, values in rate_lines(sheet.text):
            low = clean_label(label)
            if "conservation incentive adjustment" not in low or not values:
                continue
            if "over baseline" in low:
                over = values[0]
            elif "baseline" in low:
                within = values[0]
    if within is None or over is None:
        return {}

    quantities: dict[str, dict[str, dict[str, float]]] = {"basic": {}, "all_electric": {}}
    for sheet in sheets:
        if "BASELINE QUANTITIES" not in sheet.text:
            continue
        for raw in sheet.text.splitlines():
            line = raw.strip().rstrip("|").strip()
            m = re.match(r"^([P-Z])\s+((?:[0-9]+\.[0-9]+\s*(?:\([IRNL]\))?\s*){4})$", line)
            if not m:
                continue
            territory = m.group(1)
            nums = [float(x) for x in re.findall(r"[0-9]+\.[0-9]+", m.group(2))]
            if len(nums) != 4:
                continue
            quantities["basic"][territory] = {"summer": nums[0], "winter": nums[1]}
            quantities["all_electric"][territory] = {"summer": nums[2], "winter": nums[3]}

    baseline: dict[str, Any] = {
        "within_rate": within,
        "over_rate": over,
        "credit": round(over - within, 5),
    }
    if quantities["basic"]:
        baseline["quantities"] = quantities
    adders["conservation_incentive_adjustment"] = over
    return baseline


def extract(sheets: list[Page]) -> Extracted:
    unbundled = next((s for s in sheets if "UNBUNDLING" in s.text.upper()), None)
    if unbundled is None:
        raise ExtractionError("no 'UNBUNDLING OF ... TOTAL RATES' table found")
    periods, energy, adders = extract_unbundled(unbundled)
    baseline = extract_baseline(sheets, adders)
    result = Extracted(
        periods=periods,
        energy=energy,
        adders=adders,
        totals=extract_totals(sheets, periods),
        base_services_charge=extract_base_services_charge(sheets),
        pcia_vintages=extract_pcia(sheets),
        baseline=baseline,
    )
    result.rates_effective = unbundled.effective
    result.rates_advice = unbundled.advice_letter
    for sheet in sheets:
        if sheet.advice_letter and sheet.sheet_number is not None:
            result.provenance.append((sheet.sheet_number, sheet.advice_letter, sheet.effective))
    return result


def pick_effective(data: Extracted) -> date:
    """When these rates took force, per the sheet that publishes them.

    Deliberately *not* the latest date across the book. A tariff book reissues
    sheets independently: on all three schedules the totals page carries Advice
    7921-E effective 2026-06-01 while the unbundled table the rates come from
    carries 7846-E effective 2026-03-01, and the two reconcile exactly, so the
    values have been in force since March.

    The distinction is not cosmetic. ``effective`` is what ``load_snapshot``
    resolves against -- the latest snapshot on or before the moment being priced
    -- so dating a snapshot later than its rates took force leaves a hole where
    an April bill either prices from superseded rates or fails outright.
    """
    if data.rates_effective is None:
        raise ExtractionError("the unbundled sheet carries no effective date")
    return data.rates_effective


def require_provenance(data: Extracted) -> None:
    """The rate sheet has to identify itself.

    Falling back to the previous snapshot's advice letter would let a provenance
    parse failure through, and the emitted file would then claim a revision it
    was not built from -- worse than not building it, because the claim looks
    authoritative.
    """
    if not data.rates_advice:
        raise ExtractionError(
            "the unbundled sheet carries no advice letter; refusing to inherit the "
            "previous snapshot's, which would misreport where these rates came from"
        )


#: Rates are published to five decimals, and the total is rounded independently
#: of the components, so a sum can miss it by one unit in the last place. That is
#: the source's arithmetic, not a misread: a parser reads the digits that are
#: printed, so it cannot be off by exactly one ulp the way a typist can. Anything
#: larger is a real disagreement.
ROUNDING_TOLERANCE = 1.5e-5


def verify(data: Extracted) -> list[str]:
    """Every season/period must reconcile the components against the total.

    Every adder counts, the Conservation Incentive Adjustment included: E-TOU-C's
    published total is the *over-baseline* price, and the baseline credit is
    printed as a separate line rather than folded into it. Excluding the CIA left
    all four E-TOU-C cells short by exactly 0.05354, which is how that was found.
    """
    problems: list[str] = []
    flat = sum(data.adders.values())
    for season, by_period in sorted(data.energy.items()):
        for period, components in sorted(by_period.items()):
            got = sum(components.values()) + flat
            want = data.totals.get(season, {}).get(period)
            if want is None:
                problems.append(f"{season}.{period}: no published total to check against")
            elif abs(got - want) > ROUNDING_TOLERANCE:
                problems.append(
                    f"{season}.{period}: components sum to {got:.5f}, sheet says {want:.5f}"
                )
    return problems


def rounding_notes(data: Extracted) -> list[str]:
    """Cells that reconcile only within the published rounding.

    Reported rather than silently accepted: a vintage where several cells miss
    by an ulp is still fine, but it is worth seeing, because a systematic drift
    would show up here first.
    """
    flat = sum(data.adders.values())
    off = []
    for season, by_period in sorted(data.energy.items()):
        for period, components in sorted(by_period.items()):
            want = data.totals.get(season, {}).get(period)
            if want is None:
                continue
            delta = sum(components.values()) + flat - want
            if 1e-9 < abs(delta) <= ROUNDING_TOLERANCE:
                off.append(f"{season}.{period} ({delta:+.5f})")
    return off


def render(
    slug: str,
    data: Extracted,
    previous: dict[str, Any],
    effective: date,
    provider: Utility,
    franchise_fees: dict[int, float] | None = None,
) -> str:
    """Build the snapshot, carrying structure forward and rates from the sheet."""
    tariff_name = provider.schedule_names[slug]
    url = provider.schedules[slug].url
    lines = [
        f"# {provider.name} Schedule {tariff_name} -- retail time-of-use rates.",
        "#",
        f"# Source: {url}",
        "#",
        "# GENERATED by `nem-rates regen tariff` -- do not hand-edit the rate tables.",
        "# Regenerate with:",
        f"#     nem-rates regen tariff --provider {slug}",
        "#",
        "# A tariff book's sheets revise independently, so each carries its own",
        "# advice letter and effective date. The snapshot is dated from the sheet",
        "# carrying the unbundled rate table, which is when these values took",
        "# force -- not the latest date in the book. Sheets read:",
    ]
    for number, advice, eff in data.provenance:
        stamp = eff.isoformat() if eff else "unknown"
        lines.append(f"#     Sheet {number}: Advice {advice}, effective {stamp}")
    lines += [
        "#",
        "# The unbundled components below sum exactly to the published totals in",
        "# [totals]; regeneration refuses to write unless they do, which is what",
        "# makes a silent mis-parse essentially impossible.",
        "",
        "schema = 1",
        f'utility = "{provider.key.upper()}"',
        f'tariff = "{tariff_name}"',
        f'effective = "{effective.isoformat()}"',
        f'advice_letter = "{data.rates_advice}"',
        f'source_url = "{url}"',
        f'currency = "{previous.get("currency", "USD/kWh")}"',
        "",
        "# Structure the sheet does not publish, carried forward from the previous",
        "# snapshot. Changing any of it is a deliberate human edit.",
        f"has_baseline = {fmt(previous.get('has_baseline', False))}",
        "",
        "[seasons]",
    ]
    for key, value in previous.get("seasons", {}).items():
        lines.append(f"{key} = {fmt(value)}")
    lines += ["", "[periods]"]
    for key, value in previous.get("periods", {}).items():
        lines.append(f"{key} = {fmt(value)}")

    lines += ["", "# Season- and period-dependent components."]
    for season in ("summer", "winter"):
        for period in data.periods:
            components = data.energy.get(season, {}).get(period, {})
            if not components:
                continue
            lines.append(f"\n[energy.{season}.{period}]")
            for name in ("generation", "distribution"):
                if name in components:
                    lines.append(f"{name} = {fmt(components[name])}")

    lines += ["", "# Flat riders applied to all usage in every period and season.", "[adders]"]
    for key, value in data.adders.items():
        lines.append(f"{key} = {fmt(value)}")

    if data.baseline:
        lines += [
            "",
            "# The bill prints one baseline credit; the sheet implements it as two",
            "# Conservation Incentive Adjustment rates whose spread is that credit.",
            "[baseline]",
            f"within_rate = {fmt(data.baseline['within_rate'])}",
            f"over_rate = {fmt(data.baseline['over_rate'])}",
            f"credit = {fmt(data.baseline['credit'])}",
        ]
        quantities = data.baseline.get("quantities") or previous.get("baseline", {}).get(
            "quantities", {}
        )
        for code in ("basic", "all_electric"):
            for territory, seasons in sorted(quantities.get(code, {}).items()):
                lines.append(f"\n[baseline.quantities.{code}.{territory}]")
                for season, value in seasons.items():
                    lines.append(f"{season} = {fmt(value)}")

    lines += [
        "",
        "# Riders a CCA or Direct Access customer does not pay, because PG&E is not",
        "# supplying their generation.",
        "[cca]",
        f"drop_components = {fmt(previous.get('cca', {}).get('drop_components', []))}",
        "",
        "# Vintaged PCIA a CCA/DA customer pays instead of the bundled PCIA, keyed",
        "# by the year their generation service began.",
        "[cca.pcia_vintages]",
    ]
    pcia = data.pcia_vintages or {
        int(k): v for k, v in previous.get("cca", {}).get("pcia_vintages", {}).items()
    }
    for year in sorted(pcia):
        lines.append(f"{year} = {fmt(pcia[year])}")

    lines += [
        "",
        "# Franchise fee surcharge, which a CCA/DA customer pays and a bundled one",
        "# does not. Published in a separate schedule (E-FFS) and read from it,",
        "# because carrying it forward would let that schedule be reissued without",
        "# anything noticing -- it is live rate data on every CCA price.",
        "[cca.franchise_fee_vintages]",
    ]
    fees = franchise_fees or {
        int(k): v for k, v in previous.get("cca", {}).get("franchise_fee_vintages", {}).items()
    }
    for year, value in sorted(fees.items()):
        lines.append(f"{year} = {fmt(value)}")

    if data.base_services_charge:
        lines += ["", "[base_services_charge]", 'unit = "USD/day"']
        for key in ("tier_1", "tier_2", "tier_3"):
            lines.append(f"{key} = {fmt(data.base_services_charge[key])}")
    else:
        lines += [
            "",
            "# No daily fixed charge on this schedule at this vintage. AB 205's Base",
            "# Services Charge began 2026-03-01; before it these schedules had none,",
            "# so the section is absent rather than zeroed.",
        ]

    if previous.get("discounts"):
        lines += ["", "[discounts]"]
        for key, value in previous["discounts"].items():
            lines.append(f"{key} = {fmt(value)}")

    lines += [
        "",
        "# The sheet's own published totals; the components above sum to these.",
        "[totals]",
    ]
    for season in ("summer", "winter"):
        lines.append(f"\n[totals.{season}]")
        for period in data.periods:
            lines.append(f"{period} = {fmt(data.totals[season][period])}")

    return "\n".join(lines).replace("[totals]\n\n[totals.", "[totals.") + "\n"


def price_through_the_loader(slug: str, body: str, data: Extracted, provider: Utility) -> list[str]:
    """Price the generated snapshot with the library's own reader.

    ``render`` writes key names as string literals; ``nem_rates.tariff.retail``
    reads them back with a second, independent set of string literals. Those two
    encodings of one schema can drift apart silently -- a generator that renamed
    ``adders`` would still produce a valid-looking TOML file and the reader would
    quietly price everything without its riders.

    So rather than trusting the shape, hand the rendered text to the real
    ``RetailTariff`` and check the price it computes equals the sheet's own
    published total. That exercises seasons, period hours, energy components and
    adders through the consumer, which is the only thing that proves the two
    encodings still agree.
    """
    from nem_rates.config import Config
    from nem_rates.tariff import retail

    raw = tomllib.loads(body)
    snapshot = retail.TariffSnapshot(date.fromisoformat(raw["effective"]), raw)
    tariff = retail.RetailTariff(Config(tariff=provider.schedule_names[slug]))
    # The generated file is not on disk yet and load_snapshot reads packaged
    # data, so point the reader at what we just rendered.
    tariff.snapshot_for = lambda moment: snapshot  # type: ignore[method-assign]

    # 17:00 is in the peak period on all three schedules, in both seasons.
    problems: list[str] = []
    for season, moment in (
        ("summer", datetime(2026, 7, 15, 17, tzinfo=PACIFIC)),
        ("winter", datetime(2026, 1, 15, 17, tzinfo=PACIFIC)),
    ):
        want = data.totals[season]["peak"]
        try:
            got = tariff.price_at(moment).total
        except Exception as exc:
            problems.append(f"{season}: the library could not price the generated file: {exc}")
            continue
        if abs(got - want) > ROUNDING_TOLERANCE:
            problems.append(
                f"{season} peak: the library prices the generated file at {got:.5f}, "
                f"but the sheet publishes {want:.5f}"
            )
    return problems


def regenerate(
    provider: Utility,
    slug: str,
    pdf: Path,
    *,
    check: bool,
    cache: Path | None = None,
    refresh: bool = False,
) -> Result:
    """Rebuild one schedule's snapshot from ``pdf``.

    The franchise fee surcharge lives in a different schedule, so a second
    document is read here rather than the previous snapshot's values being
    carried forward -- those are live rate data on every CCA price, and carrying
    them meant E-FFS could be reissued with nothing noticing.
    """
    pages = read_pages(pdf)
    # An advice-letter filing covers many schedules; a tariff sheet covers one.
    # Narrowing by the sheets' own headers makes both work through one path.
    name = provider.sheet_name(slug)
    if len({m.group(1).upper() for p in pages if (m := SHEET_HEADER.search(p.text))}) > 1:
        pages = pages_for_schedule(pages, name)
    data = extract(pages)
    require_provenance(data)
    problems = verify(data)
    if problems:
        return Result(
            f"{provider.key}/{slug}",
            changed=False,
            failed=True,
            messages=(
                "REFUSING to write, components do not reconcile:",
                *[f"    {p}" for p in problems],
            ),
        )

    effective = pick_effective(data)
    fees, fees_from = _franchise_fees(provider, cache, effective, refresh=refresh)
    directory = DATA_DIR / "tariff" / provider.key / slug
    previous = _predecessor(directory, effective)
    body = render(slug, data, previous, effective, provider, fees)

    checked = sum(len(v) for v in data.energy.values())
    notes = [f"{checked} season/period cells reconcile against the published totals"]
    if rounded := rounding_notes(data):
        notes.append(
            f"{len(rounded)} of them only within the sheet's own rounding: {', '.join(rounded)}"
        )
    notes.append(
        f"{len(fees)} franchise fee vintages read from {fees_from}"
        if fees
        else "franchise fees carried forward: E-FFS could not be read"
    )
    if not data.pcia_vintages:
        # The filing does not restate it, so find the one that did rather than
        # inheriting from whichever snapshot happens to sit next to this one.
        found, source = _pcia_from_earlier_filing(provider, slug, effective, cache)
        if found:
            data.pcia_vintages.update(found)
            notes.append(f"PCIA table read from {source}, the last filing to restate it")
        else:
            notes.append(
                "PCIA table carried forward: no indexed filing restates it, so the "
                "values come from the preceding vintage rather than from a sheet"
            )
    return write_or_check(
        f"{provider.key}/{slug}",
        directory / f"{effective.isoformat()}.toml",
        body,
        check=check,
        verify=lambda text: price_through_the_loader(slug, text, data, provider),
        messages=notes,
    )


def pages_for_schedule(pages: list[Page], tariff_name: str) -> list[Page]:
    """Only the pages whose own sheet header names this schedule.

    A tariff-book PDF is one schedule, so this is a no-op there. An advice-letter
    filing is hundreds of pages covering every schedule the utility revised at
    once, and each sheet states which it belongs to -- so the same extractor
    works on both once the filing is narrowed to the schedule being rebuilt.
    """
    wanted = tariff_name.upper()
    mine = [
        page
        for page in pages
        if (match := SHEET_HEADER.search(page.text)) and match.group(1).upper() == wanted
    ]
    if not mine:
        seen = sorted({m.group(1).upper() for p in pages if (m := SHEET_HEADER.search(p.text))})
        raise ExtractionError(
            f"no sheets for {tariff_name} in this document; it carries "
            f"{', '.join(seen) if seen else 'no identifiable schedules'}"
        )
    return mine


def _pcia_from_earlier_filing(
    provider: Utility, slug: str, effective: date, cache: Path | None
) -> tuple[dict[int, float], str]:
    """Find the filing that last set the vintaged PCIA before ``effective``.

    The table is republished when it changes -- annually, with the ERRA update --
    and simply omitted from filings that do not touch it. So a rate change in
    September carries no PCIA at all, and taking the value from the neighbouring
    snapshot only works if snapshots happen to be built oldest-first. Looking it
    up instead makes the answer independent of the order things were generated
    in, which is the difference between a rule and a coincidence.

    Returns the table and the filing it came from, or empty and "" when the
    index has nothing earlier to offer.
    """
    from . import filings

    root = (cache or Path.home() / ".cache" / "nem-rates" / "regen") / "al"
    indexed = filings.load_index(root, provider.key)
    if not indexed:
        return {}, ""
    sheet = provider.sheet_name(slug)
    earlier = sorted(
        (when, entry)
        for entry in indexed.values()
        if (when := entry.effective_for(sheet)) and when <= effective
    )
    for _, entry in reversed(earlier):
        try:
            pages = pages_for_schedule(read_pages(root / f"{entry.number}.pdf"), sheet)
        except (ExtractionError, FileNotFoundError, OSError):
            continue
        table = extract_pcia(pages)
        if table:
            return table, entry.number
    return {}, ""


def _predecessor(directory: Path, effective: date) -> dict[str, Any]:
    """The snapshot in force immediately before ``effective``.

    Carried-forward values must come from the past, never the future. Taking
    whatever happened to be newest on disk meant backfilling an older vintage
    inherited the *current* one's structure -- and silently its PCIA table, when
    a filing did not restate it. The September 2025 vintage came out with
    January 2026's vintaged PCIA of 0.03492 against the 0.01163 the bill
    actually charged, which is a threefold error on that line.
    """
    if not directory.is_dir():
        return {}
    earlier: list[tuple[date, dict[str, Any]]] = []
    for entry in directory.glob("*.toml"):
        raw = tomllib.loads(entry.read_text(encoding="utf-8"))
        when = date.fromisoformat(str(raw["effective"]))
        if when < effective:
            earlier.append((when, raw))
    if earlier:
        return max(earlier, key=lambda pair: pair[0])[1]
    # Nothing older exists. Structure has to come from somewhere, so the nearest
    # later vintage is used -- but only for the shape, and the caller reports
    # which tables were carried rather than read.
    later = sorted(
        (date.fromisoformat(str(raw["effective"])), raw)
        for raw in (tomllib.loads(e.read_text(encoding="utf-8")) for e in directory.glob("*.toml"))
    )
    return later[0][1] if later else {}


def _franchise_fees(
    provider: Utility, cache: Path | None, effective: date, *, refresh: bool = False
) -> tuple[dict[int, float], str]:
    """The franchise fee surcharge in force at ``effective``, and where from.

    E-FFS is its own schedule with its own vintages, so reading the current one
    for a historical snapshot is the same mistake as inheriting a PCIA table
    from the future -- and it was: the December 2025 segment of a real bill was
    charged 0.00106 where today's sheet says 0.00060.

    So the filing index is asked which filing last set E-FFS on or before the
    date, exactly as for the PCIA. Only when the index has nothing does this
    fall back to the standalone schedule, which is by definition the current
    one, and the caller says so.
    """
    if provider.franchise_fees is None:
        return {}, ""
    from . import filings
    from .fetch import fetch

    root = cache or Path.home() / ".cache" / "nem-rates" / "regen"
    indexed = filings.load_index(root / "al", provider.key)
    entry = filings.filing_for(provider, "E-FFS", effective, indexed) if indexed else None
    if entry is not None:
        try:
            pages = pages_for_schedule(read_pages(root / "al" / f"{entry.number}.pdf"), "E-FFS")
            found = franchise.extract(pages)
            if found:
                return found, entry.number
        except (ExtractionError, FileNotFoundError, OSError):
            pass

    try:
        return (
            franchise.extract(
                read_pages(
                    fetch(
                        provider.franchise_fees,
                        root / f"{provider.key}-effs.pdf",
                        refresh=refresh,
                    )
                )
            ),
            "the current E-FFS sheet",
        )
    except (ExtractionError, OSError):
        # Not fatal, and that includes an unwritable cache: the retail rates are
        # the point of this dataset and the previous snapshot's fees are still
        # the last known-good ones, so a full disk should not stop a rate
        # change being vendored.
        return {}, ""
