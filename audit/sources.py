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

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, time, timedelta

from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.providers.pge.statements import Statement
from tariffkit.timeutil import PACIFIC, hour_floor, to_pacific

from .reconcile import SourceDelta, Tolerance

#: Most a two-decimal reading can lose per interval. The utility's own export
#: rounds each one, so a long cycle accumulates a predictable shortfall.
ROUNDING_PER_INTERVAL = 0.005


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


def peak_share(
    readings: Sequence[IntervalReading], classify: Callable[[datetime], object]
) -> float:
    """Imported energy the tariff prices at its peak rate."""
    return sum(
        r.imported for r in readings if str(classify(hour_floor(to_pacific(r.start)))) == "peak"
    )


def _printed_peak(statement: Statement) -> float | None:
    """Kilowatt-hours the statement itself says were charged at the peak rate.

    **Reported, never asserted on, because this is not yet right.** The
    statement is the only real arbiter of the time-of-use split -- it prints
    what the utility charged at each rate, and on one cycle our meter sat
    0.144 kWh from a printed 331.250 while the utility's own export sat 0.370
    the other way, which is the whole reason to prefer it to a second opinion.

    But picking those rows out is not solved. ``max`` avoids double-counting a
    quantity that prints twice, once as the energy charge and once as the
    generation credit, and thereby undercounts a cycle the utility split across
    two service agreements. Measured against the meter it produced -0.14 on one
    statement and +77.74 on a split one, which is not an attribution difference
    but a parsing one. Asserting on it failed statements for the reader's own
    error -- the mistake this comparison exists to avoid making in the other
    direction.
    """
    total = 0.0
    found = False
    for line in statement.lines():
        label = line.label.strip().lower()
        if line.quantity is None or not (line.unit or "").lower().startswith("kwh"):
            continue
        if not label.startswith("peak") and "peak" not in label.split():
            continue
        if label.startswith("off") or "off peak" in label or "off-peak" in label:
            continue
        # "Peak" and "Peak Winter" print the same quantity, once as the energy
        # charge and once as the generation credit, so the same kilowatt-hours
        # would be counted twice.
        total = max(total, line.quantity)
        found = True
    return total if found else None


def compare_sources(
    readings: Mapping[str, Sequence[IntervalReading]],
    statement: Statement,
    *,
    primary: str = "influx",
    tolerance: Tolerance | None = None,
    classify: Callable[[datetime], object] | None = None,
) -> list[SourceDelta]:
    """How the available measurements of one cycle disagree.

    Totals are not enough, which took a real statement to learn. Two sources
    agreed on a cycle's 701 kWh to within 0.2% and disagreed by 6.9 kWh about
    which hours it arrived in -- and since peak energy costs about two cents
    more per kilowatt-hour to deliver, that is real money on a bill that
    otherwise reconciles. Passing ``classify`` compares the split as well, which
    is the only way a totals check could have caught it.
    """
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
        # Two-decimal rounding can only lose so much: at most half a hundredth
        # per interval, so 2,880 quarter-hours caps out near 14 kWh. Bounding
        # the exemption matters -- unbounded, "Green Button reads low" would
        # excuse a shortfall of any size, including a genuinely missing day,
        # which is the exact failure this comparison exists to catch.
        budget = ROUNDING_PER_INTERVAL * len(series)
        expected_low = name == "green_button" and low and (base_import - other_import) <= budget
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

    if classify is not None:
        base_peak = peak_share(readings[primary], classify)

        # Against the statement first, where it prints the split. The utility
        # says how many kilowatt-hours it charged at the peak rate, which is an
        # arbiter rather than a second opinion -- and on a real cycle the meter
        # sat 0.144 kWh from the printed 331.250 while the utility's own export
        # sat 0.370 the other way. Judging our split against that export failed
        # statements for the export's error.
        printed_peak = _printed_peak(statement)
        if printed_peak is not None:
            deltas.append(
                SourceDelta(
                    left="statement peak",
                    right=f"{primary} peak",
                    imported_delta=printed_peak - base_peak,
                    exported_delta=0.0,
                    note=(
                        "kilowatt-hours the utility says it charged at the peak rate "
                        "-- reported only; see _printed_peak"
                    ),
                    significant=False,
                )
            )

        for name, series in readings.items():
            if name == primary:
                continue
            other_peak = peak_share(series, classify)
            # Reported, never asserted. Two derivations of a meter disagree
            # about which hour a kilowatt-hour landed in, and neither is the
            # authority on it: the statement above is. Asserting this made a
            # second source's own attribution error our failure.
            deltas.append(
                SourceDelta(
                    left=f"{name} peak",
                    right=f"{primary} peak",
                    imported_delta=other_peak - base_peak,
                    exported_delta=0.0,
                    note=(
                        "which hours the energy arrived in; the statement above "
                        "is what either of them is measured against"
                        if printed_peak is not None
                        else "which hours the energy arrived in, where the two rates differ"
                    ),
                    significant=False,
                )
            )

    if statement.billed_kwh is not None:
        # A statement covering more than one service agreement prints usage per
        # agreement, and once solar is interconnected one of those figures is a
        # net that can be negative. Neither is a whole-cycle quantity, so
        # comparing it against a whole-cycle meter reading measures the parsing,
        # not the meter -- it reported the 2026-06 cycle as 23.59 kWh adrift on
        # a bill that reconciles to the cent. Reported either way; asserted as a
        # disagreement only when the figure covers the whole cycle.
        whole_cycle = statement.service_agreements == 1
        deltas.append(
            SourceDelta(
                left="statement",
                right=primary,
                imported_delta=statement.billed_kwh - base_import,
                exported_delta=0.0,
                note=(
                    "what the utility says it billed"
                    if whole_cycle
                    else f"usage printed for one of {statement.service_agreements} service "
                    f"agreements, so it does not describe the whole cycle"
                ),
                significant=(whole_cycle and not allowed.kwh_ok(statement.billed_kwh, base_import)),
            )
        )
    return deltas
