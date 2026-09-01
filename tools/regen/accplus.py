"""Regenerating the ACC Plus export adder from a utility's export tariff.

The adder is not in the export-rate archive -- every row there carries
``Sector=ALL`` with no customer-class differentiation -- so it comes from the
tariff text instead, as a small table keyed by customer segment and the calendar
year of the completed interconnection application:

    Customer Segment    2023      2024      2025      2026      2027
    Residential         0.02200   0.01760   0.01320   0.00880   0.00440
    Residential Low
    Income              0.09000   0.07200   0.05400   0.03600   0.01800
    Non-Residential     Not Eligible

Two things make this table easy to misread, and both are checked. The segment
labels wrap across lines, so "Residential Low Income" arrives in pieces and its
figures sit on the third of them. And the years come from a header whose cells
are split one per line, so they cannot be assumed to be a fixed run.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .emit import DATA_DIR, Result, write_or_check
from .providers import Utility
from .sheets import ExtractionError, Page, cells, find_page, read_pages

#: The heading above the table, used to find it rather than assuming a page.
TABLE_HEADING = "Adopted Avoided Cost Calculator Plus Adder"

#: Printed segment -> the key the library reads. Anything else is reported
#: rather than dropped, so a new customer class is noticed.
SEGMENTS = {
    "residential": "residential",
    "residentiallowincome": "residential_low_income",
}

#: The adder steps down 20% a year until it reaches zero, so five years are
#: published. Fewer means the table was truncated by a bad parse.
MIN_YEARS = 3

#: Adders have run from 0.0044 to 0.09. An order of magnitude out means a figure
#: was picked up from neighbouring prose.
PLAUSIBLE = (0.0001, 0.5)


def extract(pages: list[Page]) -> dict[str, dict[int, float]]:
    """``{segment: {year: adder}}`` from the tariff's ACC Plus table."""
    page = find_page(pages, TABLE_HEADING)
    body = page.text[page.text.find(TABLE_HEADING) :]

    # Fix numbers broken by newlines (e.g. "0.0220\n0" -> "0.02200")
    body = re.sub(r"(\d)\n(\d)", r"\1\2", body)

    # The year header is split one cell per line: "2023 \n $/kWh \n 2024 ...".
    years = [int(y) for y in re.findall(r"\b(20\d{2})\s*\n?\s*\$/kWh", body)]
    if not years:
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", body[:400])]
    if len(years) < MIN_YEARS:
        raise ExtractionError(
            f"found only {len(years)} year columns in the ACC Plus table; expected "
            f"at least {MIN_YEARS}"
        )

    found: dict[str, dict[int, float]] = {}
    pending: list[str] = []
    current_key: str | None = None
    current_values: list[float] = []

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        values = cells(line)
        # A segment label may arrive over several lines before its figures.
        label_part = re.sub(r"[0-9.$()/\-]", "", line).strip()
        if not values:
            if line.lower().startswith("the adder"):
                break  # past the table, into the explanatory prose
            pending = [*pending, label_part][-3:]
            continue

        label = re.sub(r"[^a-z]", "", ("".join(pending) + label_part).lower())
        # Longest first: "residential" is a substring of "residentiallowincome",
        # so matching in declaration order files the low-income row under
        # residential and then drops it as a duplicate.
        key = next(
            (v for k, v in sorted(SEGMENTS.items(), key=lambda kv: -len(kv[0])) if k in label),
            None,
        )
        if key is not None:
            if current_key and len(current_values) >= MIN_YEARS:
                for value in current_values:
                    if not PLAUSIBLE[0] <= value <= PLAUSIBLE[1]:
                        raise ExtractionError(
                            f"{current_key}: adder {value} is outside the "
                            f"plausible range {PLAUSIBLE}"
                        )
                found.setdefault(
                    current_key,
                    dict(zip(years[: len(current_values)], current_values, strict=False)),
                )
            current_key = key
            current_values = values
            pending = []
        else:
            if current_key is not None:
                current_values.extend(values)
            pending = []

    if current_key and len(current_values) >= MIN_YEARS:
        for value in current_values:
            if not PLAUSIBLE[0] <= value <= PLAUSIBLE[1]:
                raise ExtractionError(
                    f"{current_key}: adder {value} is outside the plausible range {PLAUSIBLE}"
                )
        found.setdefault(
            current_key, dict(zip(years[: len(current_values)], current_values, strict=False))
        )

    missing = set(SEGMENTS.values()) - set(found)
    if missing:
        raise ExtractionError(f"no ACC Plus rows found for {', '.join(sorted(missing))}")
    return found


def verify_against_library(body: str, extracted: dict[str, dict[int, float]]) -> list[str]:
    """The library must read back every adder this file claims to publish.

    Checked by pricing through ``NbtExportRates``, not by looking for a constant
    by name: the point is that the consumer can still find and read these
    values, and a rename should not pass while a schema change fails.
    """
    from tariffkit.config import Config
    from tariffkit.export import nbt

    raw = tomllib.loads(body)
    problems: list[str] = []
    for segment, by_year in sorted(extracted.items()):
        table = raw.get(segment)
        if not isinstance(table, dict):
            problems.append(f"{segment}: missing from the rendered file")
            continue
        for year, value in sorted(by_year.items()):
            got = table.get(str(year), table.get(year))
            if got is None or abs(float(got) - value) > 1e-9:
                problems.append(f"{segment} {year}: rendered {got!r}, extracted {value}")

    import unittest.mock

    def mock_acc_plus_table(utility: object, on: object) -> dict[str, object]:
        return raw

    # And the consumer must agree, for a year the table covers.
    with unittest.mock.patch(
        "tariffkit.export.nbt._acc_plus_table", side_effect=mock_acc_plus_table
    ):
        for segment, by_year in sorted(extracted.items()):
            year = sorted(by_year)[0]
            try:
                rates = nbt.NbtExportRates(
                    Config(interconnection_year=year, acc_plus_segment=segment)  # type: ignore[arg-type]
                )
                if abs(rates.acc_plus - by_year[year]) > 1e-9:
                    problems.append(
                        f"{segment} {year}: the library reads {rates.acc_plus}, "
                        f"extracted {by_year[year]}"
                    )
            except Exception as exc:
                problems.append(f"{segment} {year}: the library could not read it back: {exc}")
    return problems


def render(provider: Utility, adders: dict[str, dict[int, float]], source_url: str) -> str:
    lines = [
        f"# ACC Plus adder, {provider.name} Schedule NBT.",
        "#",
        f"# Source: {source_url}",
        "#",
        "# GENERATED by `python -m tools.regen accplus` -- do not hand-edit.",
        "#",
        "# This is NOT present in the export-rate data files -- every row there",
        "# carries Sector=ALL with no customer-class differentiation -- so it is",
        "# read from the tariff and added on top.",
        "#",
        "# Keyed by the calendar year of the completed interconnection application,",
        "# then held CONSTANT for nine years from the Permission-To-Operate date.",
        "# The year-over-year decline below is a step-down for later applicants, not",
        "# a decay applied to an existing customer.",
        "#",
        "# Unlike ordinary export credits, ACC Plus applies to all charges including",
        "# non-bypassable charges, does not expire, and is cashed out on account",
        "# closure.",
        "",
        "schema = 1",
        f'effective = "{_effective_of(provider)}"',
        'unit = "USD/kWh"',
        f'source = "{provider.name} Schedule NBT"',
    ]
    for segment in ("residential", "residential_low_income"):
        lines.append(f"\n[{segment}]")
        for year, value in sorted(adders[segment].items()):
            lines.append(f"{year} = {value:.5f}")
    lines += ["", "# Non-residential customers are not eligible for ACC Plus."]
    return "\n".join(lines) + "\n"


def _effective_of(provider: Utility) -> str:
    """The date this adder table took force.

    The sheet does not print one -- it is a standing table in Schedule NBT -- so
    the existing vintage's date is kept and a new one is a deliberate human act.
    Guessing a date from the run's clock would silently re-date the table every
    time it was regenerated.
    """
    import tomllib

    directory = DATA_DIR / "export" / provider.key / "acc_plus"
    existing = sorted(directory.glob("*.toml")) if directory.is_dir() else []
    if existing:
        return str(tomllib.loads(existing[-1].read_text(encoding="utf-8"))["effective"])
    raise ExtractionError(
        f"no existing {provider.key} ACC Plus vintage to date this against. The "
        f"tariff does not print an effective date for this table, so create the "
        f"first vintage by hand with the date it took force."
    )


def regenerate(provider: Utility, pdf: Path, *, check: bool) -> Result:
    if provider.export_adder is None:
        raise ExtractionError(f"{provider.key} publishes no export adder tariff")
    adders = extract(read_pages(pdf))
    counted = sum(len(v) for v in adders.values())
    body = render(provider, adders, provider.export_adder.url)
    # Effective-dated: a revision sits beside its predecessor rather than
    # overwriting it, so a bill from before the revision still prices.
    effective = _effective_of(provider)
    target = DATA_DIR / "export" / provider.key / "acc_plus" / f"{effective}.toml"
    return write_or_check(
        f"{provider.key}/accplus",
        target,
        body,
        check=check,
        verify=lambda text: verify_against_library(text, adders),
        messages=[f"{counted} adders across {len(adders)} customer segment(s)"],
    )
