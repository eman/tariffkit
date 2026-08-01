"""Pacific-time primitives shared by the import and export engines.

Everything PG&E publishes is anchored to Pacific Prevailing Time, so the whole
package normalizes to it exactly once, here, and works in wall-clock terms
downstream.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

from .errors import DataError

PACIFIC = ZoneInfo("America/Los_Angeles")

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class DayType(StrEnum):
    """Export day type. E-ELEC import pricing does not use this at all."""

    WEEKDAY = "Weekday"
    WEEKEND = "Weekend"


def to_pacific(moment: datetime) -> datetime:
    """Convert an aware datetime to Pacific time.

    Naive datetimes are rejected rather than assumed: silently guessing a zone
    is how an export lookup ends up eight hours off.
    """
    if moment.tzinfo is None:
        raise ValueError("naive datetime; pass an aware one, e.g. datetime.now(nem_rates.PACIFIC)")
    return moment.astimezone(PACIFIC)


def now_pacific() -> datetime:
    return datetime.now(PACIFIC)


def hour_floor(moment: datetime) -> datetime:
    """Truncate to the start of the containing hour, preserving the offset."""
    return moment.replace(minute=0, second=0, microsecond=0)


def next_hour(moment: datetime) -> datetime:
    """The start of the next hour.

    Uses absolute-time arithmetic so it stays correct across DST transitions:
    on the fall-back day 01:00 PDT + 1h is 01:00 PST, a real distinct hour.
    """
    return hour_floor(moment) + timedelta(hours=1)


@lru_cache(maxsize=1)
def _holiday_table() -> dict[int, frozenset[date]]:
    from .data import read_data_text

    raw = tomllib.loads(read_data_text("holidays.toml"))["holidays"]
    return {
        int(year): frozenset(date.fromisoformat(d) for d in dates) for year, dates in raw.items()
    }


def holidays(year: int) -> frozenset[date]:
    """The eight tariff holidays observed in ``year``.

    Extracted from PG&E's own data rather than recomputed, so the observed-date
    rules (Saturday -> preceding Friday, Sunday -> following Monday) and the
    year-spanning New Year's case come from the source of truth.
    """
    table = _holiday_table()
    if year not in table:
        raise DataError(
            f"no holiday calendar for {year}; vendored data covers {min(table)}-{max(table)}"
        )
    return table[year]


def is_holiday(day: date) -> bool:
    return day in holidays(day.year)


def export_hour(moment: datetime) -> int:
    """The hour index (``HS0``-``HS23``) upstream assigns to this instant.

    Almost always the wall-clock hour, but the fall-back day has 25 real hours
    and only 24 labels. PG&E resolves that by giving the repeated 01:00 PST the
    ``HS2`` label, so the second 1am is priced as 2am and the rest of the day
    stays aligned. ``fold`` is 1 only during that ambiguous hour, so adding it
    reproduces the published labelling exactly.

    The spring-forward day needs no special case: 02:00 simply does not exist,
    and upstream correspondingly publishes no ``HS2`` row for it.
    """
    return moment.hour + moment.fold


def day_type(moment: datetime, calendar: frozenset[date] | None = None) -> DayType:
    """Classify a Pacific-time moment for export-rate lookup.

    Saturdays, Sundays, and the eight tariff holidays all share the "Weekend"
    matrix column.

    ``calendar`` overrides the shared holiday table. Export lookups pass the
    calendar embedded in their own vintage file: the vintages disagree about
    far-future holidays, and using the wrong one reads the wrong matrix column.
    """
    if moment.weekday() >= 5:
        return DayType.WEEKEND
    day = moment.date()
    holiday = day in calendar if calendar is not None else is_holiday(day)
    return DayType.WEEKEND if holiday else DayType.WEEKDAY
