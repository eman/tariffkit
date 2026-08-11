"""Netting rules and data-quality checks.

A bill computed over a lossy interval series is silently wrong -- it simply
looks like a month with less usage. So coverage problems are surfaced as
warnings on the bill rather than being papered over by interpolation.

They do not clear ``Bill.complete``, which is a claim about the rates rather
than the readings: a bill can reconcile against a real statement and still carry
coverage warnings. Callers wanting "trust this total" check both.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from ..timeutil import to_pacific
from .models import BillingPeriod, IntervalReading

#: Fraction of a period that may be unaccounted for before it is reported.
COVERAGE_TOLERANCE = 0.01


def check_coverage(readings: Sequence[IntervalReading], period: BillingPeriod) -> Iterator[str]:
    """Report ways the readings fail to cleanly cover ``period``.

    Yields human-readable problems. An empty result means the series is
    contiguous, non-overlapping, and spans the whole cycle.
    """
    if not readings:
        yield f"no readings in {period.start}..{period.end}"
        return

    ordered = sorted(readings, key=lambda r: to_pacific(r.start))

    covered = sum((r.duration for r in ordered), timedelta())
    # Real elapsed time, not days x 24: a cycle spanning a DST transition is an
    # hour longer or shorter, and on the autumn one that difference hides an
    # hour of genuinely missing data.
    expected = period.elapsed
    shortfall = expected - covered
    if shortfall > expected * COVERAGE_TOLERANCE:
        yield (
            f"readings cover {covered.total_seconds() / 3600:.1f}h of the "
            f"{expected.total_seconds() / 3600:.0f}h period "
            f"({shortfall.total_seconds() / 3600:.1f}h missing)"
        )

    gaps = list(find_gaps(ordered))
    if gaps:
        first = gaps[0]
        yield (
            f"{len(gaps)} gap(s) in the series; first from "
            f"{first[0].isoformat()} to {first[1].isoformat()}"
        )

    overlaps = list(find_overlaps(ordered))
    if overlaps:
        yield f"{len(overlaps)} overlapping interval(s); first at {overlaps[0].isoformat()}"

    both = [r for r in ordered if r.imported and r.exported]
    if both:
        yield (
            f"{len(both)} interval(s) report both import and export; the meter "
            f"nets within an interval, so this suggests gross data that was not "
            f"netted (use IntervalReading.from_gross)"
        )


def _elapsed(moment: datetime) -> datetime:
    """The instant, so arithmetic measures real time rather than clock face.

    Adding a duration to a zoned datetime advances the wall clock, which on a DST
    transition is not the same as advancing time. Both transitions get it wrong
    and in opposite directions: on the autumn day an hour missing from the data
    is hidden, because 01:45 plus fifteen minutes reads as 02:00 and the clock
    has meanwhile gone back; on the spring day a contiguous series looks
    discontinuous, because the labels jump an hour that never existed.
    """
    return to_pacific(moment).astimezone(UTC)


def find_gaps(readings: Sequence[IntervalReading]) -> Iterator[tuple[datetime, datetime]]:
    """Yield (gap_start, gap_end) for each discontinuity, in order."""
    ordered = sorted(readings, key=lambda r: _elapsed(r.start))
    for earlier, later in pairwise(ordered):
        expected = _elapsed(earlier.start) + earlier.duration
        actual = _elapsed(later.start)
        if actual > expected:
            yield (to_pacific(expected), to_pacific(actual))


def find_overlaps(readings: Sequence[IntervalReading]) -> Iterator[datetime]:
    """Yield the start of each interval that begins before its predecessor ends."""
    ordered = sorted(readings, key=lambda r: _elapsed(r.start))
    for earlier, later in pairwise(ordered):
        if _elapsed(later.start) < _elapsed(earlier.start) + earlier.duration:
            yield to_pacific(later.start)


def net_intervals(readings: Iterable[IntervalReading]) -> list[IntervalReading]:
    """Net import against export within each interval.

    A no-op for real AMI data, which the meter has already netted. Meaningful
    only for series assembled from separate consumption and production feeds,
    where an interval can carry both.

    Netting granularity is a real tariff question, not a formatting choice: the
    finer the interval, the less self-consumption offsets, and the higher the
    bill. This nets at whatever granularity the readings arrive in, which is the
    honest default -- it does not invent a coarser or finer one.
    """
    return [
        IntervalReading.from_net(r.start, r.net, r.duration) if r.imported and r.exported else r
        for r in readings
    ]
