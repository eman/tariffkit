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

from tariffkit.config import VINTAGE_BY_YEAR, Config
from tariffkit.export.nbt import NbtExportRates
from tariffkit.timeutil import PACIFIC, DayType, day_type, export_hour

FIXTURE = Path(__file__).parent / "fixtures" / "nbt_golden_rows.jsonl"

VINTAGE_TO_YEAR = {
    "NBT23": 2023,
    "NBT24": 2024,
    "NBT25": 2025,
    "NBT26": 2026,
    "NBT27": 2027,
    "NBT28": 2028,
}


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
    for required in (
        "2026-03-08",
        "2026-11-01",
        "2026-07-03",
        "2026-12-31",
        "2027-01-01",
        # The 2027 vintage's first fall-back day, labelled HS1 rather than HS2.
        "2027-11-07",
        "2028-11-05",
    ):
        assert required in pacific_dates


def test_fixture_covers_both_fall_back_labels() -> None:
    """PG&E has labelled the repeated 01:00 both ways; both must stay tested."""
    labels = {
        (r["vintage"], r["value_name"].split()[2])
        for r in golden_rows()
        if (moment := parse_utc(r["date_start"], r["time_start"]).astimezone(PACIFIC)).fold
        and moment.utcoffset() != moment.replace(fold=0).utcoffset()
    }
    assert ("NBT26", "HS2") in labels
    assert ("NBT27", "HS1") in labels
    # The floating file carries both: 2026 kept from the earlier copy, 2027 on.
    assert {("NBT00", "HS2"), ("NBT00", "HS1")} <= labels


@pytest.mark.parametrize(
    ("vintage", "year", "label"),
    [("NBT26", 2026, 2), ("NBT27", 2027, 1), ("NBT00", 2026, 2), ("NBT00", 2027, 1)],
)
def test_the_repeated_hour_prices_from_its_years_label(vintage: str, year: int, label: int) -> None:
    """The second 01:00 reads the cell its own year's file says it does."""
    from datetime import datetime as dt

    rates = rates_for(vintage)
    first_sunday = next(d for d in range(1, 8) if dt(year, 11, d).weekday() == 6)
    repeated = dt(year, 11, first_sunday, 1, tzinfo=PACIFIC, fold=1)
    assert repeated.utcoffset() != repeated.replace(fold=0).utcoffset()
    expected = rates.month_curve(year, "Nov", DayType.WEEKEND)[label]
    assert rates.price_at(repeated).total == pytest.approx(expected)


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
    fall_back_hour = rates._payload["fall_back_hour"].get(str(pacific.year), 2)
    assert export_hour(pacific, fall_back_hour) == int(hour_label.removeprefix("HS"))
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


@pytest.mark.parametrize("year", sorted(VINTAGE_BY_YEAR))
def test_every_vintage_is_exact_through_a_late_ptos_lock(year: int) -> None:
    """The same, for each vintage an interconnection year selects.

    PTO a full year after the application is late but not unusual, and pushes
    the lock end as far as it goes. NBT27 arrived reporting nothing as exact,
    which this would have caught for the vintage most new systems now get.
    """
    from datetime import date

    config = Config(interconnection_year=year, pto_date=date(year + 1, 12, 31))
    lock_end = config.lock_end
    assert lock_end is not None
    assert lock_end.year <= NbtExportRates(config).exact_through


@pytest.mark.parametrize("supplier", ["bundled", "cca"])
def test_the_month_curve_is_the_price_it_plots(supplier: str) -> None:
    """Each hour of the curve equals what ``price_at`` totals for that hour.

    Untested until now, and it had drifted: on a CCA account the curve was the
    delivery component plus the ACC Plus adders, omitting the CCA's generation
    credit and its bonuses -- a figure neither party pays, plotted as though it
    were the export price. Both are built from one component rule now, and this
    is what says they still are.
    """
    from datetime import date

    from tariffkit.config import CcaConfig
    from tariffkit.models import Supplier

    config = (
        Config(
            tariff="E-ELEC",
            supplier=Supplier.CCA,
            cca=CcaConfig(name="MCE", rate_card="mce"),
            interconnection_year=2026,
            pto_date=date(2026, 1, 1),
        )
        if supplier == "cca"
        else Config()
    )
    rates = NbtExportRates(config)
    curve = rates.month_curve(2026, "Jul", DayType.WEEKDAY)
    # 2026-07-15 is a Wednesday, so every hour of it is a weekday hour.
    hourly = [
        rates.price_at(datetime(2026, 7, 15, hour, tzinfo=PACIFIC)).total for hour in range(24)
    ]
    assert list(curve) == pytest.approx(hourly)
