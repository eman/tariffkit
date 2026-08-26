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


def check_coverage(
    readings: Sequence[IntervalReading],
    period: BillingPeriod,
    *,
    netted: bool = False,
    require_full_span: bool = True,
) -> Iterator[str]:
    """Report ways the readings fail to cleanly cover ``period``.

    Yields human-readable problems. An empty result means the series is
    contiguous, non-overlapping, and spans the whole cycle.

    ``netted`` says the caller already knows these readings come from a meter's
    own import and export registers, which net at the meter's interval and
    legitimately leave both non-zero once aggregated to a coarser one. Without
    it that shape is reported, because for gross inverter or CT data it is a
    real error -- but for a caller who knows better the report is noise, and
    noise that never clears trains its readers to ignore the warnings that
    matter. Declaring the fact is what this flag is for; matching on the text of
    the message is not.

    ``require_full_span`` is for a period still in progress. A running total for
    today has not covered the rest of the day and never claimed to, so the
    elapsed shortfall says only that time has not passed yet. Every other check
    stays on: a gap mid-morning is a gap whether or not the day has finished.
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
    if require_full_span and shortfall > expected * COVERAGE_TOLERANCE:
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

    guessed = [r for r in ordered if r.estimated]
    if guessed:
        energy = sum(r.imported + r.exported for r in guessed)
        hours = sum((r.duration for r in guessed), timedelta()).total_seconds() / 3600
        yield (
            f"{len(guessed)} interval(s) covering {hours:.1f}h and {energy:.1f} kWh were "
            f"reconstructed across gaps in the source, so their time-of-use split is a "
            f"guess even though the cycle total is not. Spreading a long gap evenly gives "
            f"peak hours their share of the clock rather than their share of the load"
        )

    both = [] if netted else [r for r in ordered if r.imported and r.exported]
    if both:
        # Deliberately does not tell the reader to net. It used to, and the
        # advice is wrong for the commonest source: a meter's own import and
        # export registers are already netted at the meter's interval, and
        # aggregating them to a coarser one legitimately leaves both non-zero.
        # Netting again is double-netting, which the tariff does not do --
        # measured against a real Solar Billing Plan statement it moved the
        # cycle from three cents out to forty. Only independently metered gross
        # sources, an inverter or a CT clamp, want `IntervalReading.from_gross`.
        yield (
            f"{len(both)} interval(s) report both import and export. Expected when "
            f"already-netted meter registers are aggregated to a coarser interval; "
            f"a sign of un-netted gross data only if these are inverter or CT "
            f"readings, which want IntervalReading.from_gross"
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
