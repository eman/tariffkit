"""Resample an hourly curve onto shorter intervals.

Prices are piecewise-constant across the clock hour -- the tariff sheet assigns
one rate to the whole hour and the NBT matrix is indexed by hour -- so splitting
an hour into shorter slots repeats a value that genuinely holds for all of them.
This is presentation, not interpolation, and no finer data exists upstream.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..models import PriceCurve, PricePoint
from ..timeutil import PACIFIC


def local_day_window(moment: datetime, days: int = 2) -> tuple[datetime, int]:
    """Local midnight starting the window, and how many absolute hours it spans.

    Consumers that think in calendar days need a curve anchored to midnight, not
    to the current hour. The hour count is derived rather than assumed 24/day:
    the fall-back day spans 25 and the spring-forward day 23.
    """
    midnight = moment.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    # Wall-clock arithmetic is what's wanted here -- the window must end on a
    # local midnight, whatever number of real hours away that lands.
    horizon = midnight + timedelta(days=days)
    hours = int((horizon.astimezone(UTC) - midnight.astimezone(UTC)) / timedelta(hours=1))
    return midnight, hours


def resample(curve: PriceCurve, minutes: int = 30) -> tuple[PricePoint, ...]:
    """Split every point in ``curve`` into ``minutes``-long slots.

    Returns a plain tuple rather than a :class:`PriceCurve` because that type's
    ``to_dict`` reports a ``hours`` count, which stops being true below the hour.

    Boundaries are stepped in UTC, so the 23- and 25-hour days resample without
    landing on a nonexistent wall clock, and the two 01:00 hours of the fall-back
    day keep their distinct offsets.
    """
    if minutes < 1 or 60 % minutes:
        raise ValueError("minutes must be a positive divisor of 60")
    if minutes == 60:
        return curve.points

    step = timedelta(minutes=minutes)
    slots: list[PricePoint] = []
    for point in curve:
        cursor = point.start.astimezone(UTC)
        limit = point.end.astimezone(UTC)
        while cursor < limit:
            nxt = min(cursor + step, limit)
            slots.append(
                PricePoint(
                    start=cursor.astimezone(PACIFIC),
                    end=nxt.astimezone(PACIFIC),
                    import_price=point.import_price,
                    export_price=point.export_price,
                )
            )
            cursor = nxt
    return tuple(slots)
