"""Getting the interval data a cycle was billed on.

More than one source is read on purpose. PG&E's own Green Button export was once
missing a whole day -- 2026-01-28, twenty-four hours of it -- and nothing about
the file said so; it was only visible because a second meter disagreed. A single
source cannot tell you it is incomplete.

The comparison is directional, not just magnitudinal. Green Button rounds each
interval to two decimals and so reads slightly *low* over a month; that is a
property of the export, not a defect, and reporting it as one would train the
reader to ignore the line. Green Button reading *high*, or either source
disagreeing with the total the statement itself prints, is a real signal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta

from nem_rates.billing import BillingPeriod, IntervalReading
from nem_rates.timeutil import PACIFIC

from .reconcile import SourceDelta, Tolerance
from .statements import Statement


def window(period: BillingPeriod, *, read_hour: int = 0) -> tuple[datetime, datetime]:
    """The half-open instant range a billing period covers.

    ``read_hour`` exists because the meter is not read at midnight. A cycle read
    at 14:00 means the hours either side belong to the adjacent cycle, which on a
    thousand-kilowatt-hour month is a few dollars: large enough to look like a
    defect, small enough to be dismissed as rounding. Making it a parameter means
    a mismatch can be tested against it rather than argued about.
    """
    start = datetime.combine(period.start, time(read_hour), PACIFIC)
    end = datetime.combine(period.end + timedelta(days=1), time(read_hour), PACIFIC)
    return start, end


def totals(readings: Sequence[IntervalReading]) -> tuple[float, float]:
    return sum(r.imported for r in readings), sum(r.exported for r in readings)


def compare_sources(
    readings: Mapping[str, Sequence[IntervalReading]],
    statement: Statement,
    *,
    primary: str = "influx",
    tolerance: Tolerance | None = None,
) -> list[SourceDelta]:
    """How the available measurements of one cycle disagree."""
    allowed = tolerance or Tolerance()
    deltas: list[SourceDelta] = []
    if primary not in readings:
        return deltas

    base_import, base_export = totals(readings[primary])

    for name, series in readings.items():
        if name == primary:
            continue
        other_import, other_export = totals(series)
        low = other_import <= base_import
        expected_low = name == "green_button" and low
        deltas.append(
            SourceDelta(
                left=name,
                right=primary,
                imported_delta=other_import - base_import,
                exported_delta=other_export - base_export,
                note=(
                    "Green Button rounds each interval to two decimals, so reading "
                    "a little low is the export's own behaviour"
                    if expected_low
                    else ""
                ),
                significant=not expected_low and not allowed.kwh_ok(other_import, base_import),
            )
        )

    if statement.billed_kwh is not None:
        deltas.append(
            SourceDelta(
                left="statement",
                right=primary,
                imported_delta=statement.billed_kwh - base_import,
                exported_delta=0.0,
                note="what the utility says it billed",
                significant=not allowed.kwh_ok(statement.billed_kwh, base_import),
            )
        )
    return deltas
