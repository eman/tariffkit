"""Round-trip the vendored matrices against real rows from PG&E's own files.

The vendored data is a 300x collapse of upstream's hourly expansion. These rows
were sampled verbatim from the published CSVs -- deliberately including both DST
transitions, an observed holiday, and the Pacific/UTC year boundary -- so if the
collapse ever loses or misplaces a value, this fails.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nem_rates.config import Config
from nem_rates.export.nbt import NbtExportRates
from nem_rates.timeutil import PACIFIC, DayType, day_type, export_hour

FIXTURE = Path(__file__).parent / "fixtures" / "nbt_golden_rows.jsonl"

VINTAGE_TO_YEAR = {"NBT23": 2023, "NBT24": 2024, "NBT25": 2025, "NBT26": 2026}


def golden_rows() -> list[dict[str, str]]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def rates_for(vintage: str) -> NbtExportRates:
    return NbtExportRates(
        Config(
            vintage=vintage,
            interconnection_year=VINTAGE_TO_YEAR.get(vintage),
            acc_plus_segment="none",
        )
    )


def parse_utc(date_str: str, time_str: str) -> datetime:
    month, day, year = (int(p) for p in date_str.split("/"))
    hour, minute, second = (int(p) for p in time_str.split(":"))
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_fixture_covers_the_tricky_dates() -> None:
    """Guard the guard: a fixture that lost its edge cases proves nothing."""
    pacific_dates = {
        parse_utc(r["date_start"], r["time_start"]).astimezone(PACIFIC).date().isoformat()
        for r in golden_rows()
    }
    for required in ("2026-03-08", "2026-11-01", "2026-07-03", "2026-12-31", "2027-01-01"):
        assert required in pacific_dates


@pytest.mark.parametrize("row", golden_rows(), ids=lambda r: f"{r['vintage']}-{r['value_name']}")
def test_matrix_reproduces_upstream_row(row: dict[str, str]) -> None:
    rates = rates_for(row["vintage"])
    pacific = parse_utc(row["date_start"], row["time_start"]).astimezone(PACIFIC)

    low, high = rates.covered_years
    if not low <= pacific.year <= high:
        pytest.skip(f"{pacific.year} outside vendored coverage for {row['vintage']}")
    if pacific.year > rates.exact_through:
        pytest.skip(f"{pacific.year} past exact_through={rates.exact_through} for {row['vintage']}")

    price = rates.price_at(pacific)
    # PGXX rows are the delivery component; XXPG rows are generation.
    component = "delivery" if row["rin"].split("-")[1] == "PGXX" else "generation"
    assert price.components[component] == pytest.approx(float(row["value"]), abs=1e-9)


@pytest.mark.parametrize("row", golden_rows(), ids=lambda r: f"{r['vintage']}-{r['value_name']}")
def test_day_type_and_hour_agree_with_upstream_labels(row: dict[str, str]) -> None:
    """Our day-type and hour derivation must match upstream's own labels."""
    rates = rates_for(row["vintage"])
    pacific = parse_utc(row["date_start"], row["time_start"]).astimezone(PACIFIC)
    if pacific.year > rates.exact_through:
        pytest.skip(f"{pacific.year} past exact_through for {row['vintage']}")
    month_name, upstream_day_type, hour_label = row["value_name"].split()

    assert pacific.strftime("%b") == month_name
    assert export_hour(pacific) == int(hour_label.removeprefix("HS"))
    assert str(day_type(pacific, rates._holidays)) == upstream_day_type

    # Upstream flags holidays as day 8; those must land in the Weekend column.
    if row["day_start"] == "8":
        assert day_type(pacific, rates._holidays) is DayType.WEEKEND


def test_lock_window_falls_inside_the_exactly_verified_range() -> None:
    """The years that actually matter must be the ones we verified exactly.

    Anything past ``exact_through`` is illustrative and may be off by an hour
    slot; a rate lock extending into that range would be a real problem.
    """
    config = Config()  # the shipped defaults: NBT26, PTO 2026-06-03
    rates = NbtExportRates(config)
    lock_end = config.lock_end
    assert lock_end is not None
    assert lock_end.year <= rates.exact_through
