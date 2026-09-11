"""Turning cumulative meter counters into interval energy.

Pure arithmetic over readings, with no idea where they came from. It lived
inside the Home Assistant client because that was the first caller, which meant
the Home Assistant *integration* imported a websocket client to reuse three
functions that make no network calls -- and the InfluxDB client kept its own
half of the same idea.

A meter reader publishes a counter that only climbs, and republishes ``0.0``
while it re-establishes its session with the meter. Differencing across that
invents a huge interval and then a compensating hole, so the artefact has to be
recognised rather than billed, and every client that reads a counter needs the
same rule.
"""

from __future__ import annotations

from datetime import datetime

#: Ceiling on implied power for one interval, in kW. Anything above it is a
#: counter artefact rather than energy.
#:
#: Statistics restart their running ``sum`` when recording is interrupted, and
#: the first point of the new epoch reports the whole accumulated total as its
#: ``change``. One real instance put 543.663 kWh in a five-minute slot -- about
#: 6,500 kW, against a 200 A service that tops out near 48 kW. Set well above any
#: residential service so it only ever catches the impossible.
MAX_INTERVAL_KW = 100.0


def carry(
    previous: tuple[float, float] | None, slot: float, state: float | None
) -> tuple[float, float] | None:
    """The last usable ``(slot, counter)`` pair, given this row.

    A row whose ``state`` is zero or missing is the artefact itself, so it is
    not what the next row should difference against -- the previous good reading
    is, and keeping it is what lets a single spoiled interval be repaired rather
    than propagating.
    """
    if state is None:
        return previous
    value = float(state)
    return (slot, value) if value > 0 else previous


def interval_energy(
    change: float | None,
    state: float | None,
    previous: tuple[float, float] | None,
    slot: float,
    step: float,
    max_kw: float = MAX_INTERVAL_KW,
) -> float | None:
    """One interval's energy, repairing what the recorder spoiled.

    ``change`` is what the recorder believes the counter advanced by, and it is
    wrong whenever the source dropped to zero: a ``total_increasing`` sensor
    reading 0.0 is taken for a counter reset, so the next interval's ``change``
    carries the whole counter -- 1455 kWh on a meter that had moved 0.003. The
    A meter reader does this several times a day while it re-establishes
    its meter session.

    Refusing that row is right and dropping the interval with it is not. The
    true figure is still in ``state``, which is the counter itself: difference
    it against the previous interval and the energy comes back. On a real
    account that recovered 14.1 kWh of a cycle's 68.3 across 56 hours.

    Only across *consecutive* intervals. A gap means the counter also advanced
    through intervals nobody recorded, and crediting that whole advance to the
    interval the series resumes would price hours of energy at one interval's
    time-of-use rate -- worse than the hole, and confidently so.

    ``previous`` is the last usable ``(slot, state)`` pair for this entity, as
    :func:`carry` maintains it. Slots and ``step`` are in seconds.

    :func:`monotonic` below is the same repair one layer
    down, applied to raw samples rather than to recorded intervals, and its rule
    is the same: a reading that is zero or below one already seen is a device
    artefact and not energy.
    """
    ceiling = max_kw * step / 3600
    if change is not None and 0 <= change <= ceiling:
        return change
    if state is None or state <= 0 or previous is None:
        return None
    was_at, was = previous
    if abs(slot - was_at - step) > 1.0:
        return None
    advance = state - was
    return advance if 0 <= advance <= ceiling else None


def monotonic(samples: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Drop readings that cannot be a cumulative counter moving forward.

    A meter reader re-establishes its session with the meter several times a
    day and publishes exactly ``0.0`` while it does -- about one sample in ten
    on the data this was written against. A reading that is zero, negative, or
    lower than one already seen is a
    device artefact, not energy, and differencing across it would invent a huge
    interval and then a compensating hole.

    This is the same rule the Home Assistant template filter applies, reproduced
    here so the unfiltered series -- which reaches back nine months further --
    can be used directly.

    KNOWN LIMITATION, deliberately not papered over: a counter that *restarts*
    at a lower base -- a meter swap, a firmware reset, a 32-bit wrap -- leaves
    every later sample below the old maximum, so this discards the remainder of
    the window and the bill comes out short and plausible. Detecting it here
    was tried and withdrawn: a rule strong enough to catch a noisy restart also
    fired on a single spuriously *high* sample, which poisons the maximum and
    makes every subsequent normal reading look like a restart. Turning that
    into a hard error broke legitimate reads, which on the Home Assistant side
    means every entity goes unavailable. Separating the two cases needs
    upward-outlier rejection this does not have, so the artefact rule stands
    and the gap is recorded rather than half-closed.
    """
    kept: list[tuple[datetime, float]] = []
    highest: float | None = None
    for moment, value in samples:
        if value is None or value <= 0:
            continue
        if highest is not None and value < highest:
            continue
        highest = value
        kept.append((moment, value))
    return kept
