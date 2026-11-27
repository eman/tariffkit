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
from dataclasses import replace
from datetime import datetime, time, timedelta

from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.providers.pge.statements import Statement
from tariffkit.timeutil import PACIFIC, hour_floor, to_pacific

from .reconcile import SourceDelta, Tolerance

#: Most a two-decimal reading can lose per interval. The utility's own export
#: rounds each one, so a long cycle accumulates a predictable shortfall.
ROUNDING_PER_INTERVAL = 0.005

#: The key the utility's own export is filed under, whether it is a comparison
#: or the source the bill was priced from.
GREEN_BUTTON = "green_button"

#: The export's timing at InfluxDB's totals. See :func:`reshaped`.
GREEN_BUTTON_SHAPED = "green_button:shaped"


def within(
    readings: Sequence[IntervalReading], start: datetime, end: datetime
) -> list[IntervalReading]:
    """The readings that start inside ``[start, end)``.

    The export cache answers a narrow request with a wider file when it has
    one, and pricing ignores readings outside the cycle -- but a comparison of
    totals does not, so an unclipped year-long file would be reported as a
    meter hundreds of kilowatt-hours adrift.
    """
    return [r for r in readings if start <= r.start < end]


def reshaped(
    shape: Sequence[IntervalReading], totals_from: Sequence[IntervalReading]
) -> list[IntervalReading]:
    """``shape``'s timing at ``totals_from``'s totals, import and export each.

    Separates the two things a meter record can get wrong. The utility's export
    says which hours the energy arrived in but rounds every read, so it comes up
    short over a cycle; InfluxDB's cycle totals match what the statement bills,
    but its hours are partly reconstructed across sample gaps. Pricing one at
    the other's totals asks what the bill would be if the timing were the
    utility's and the quantity ours -- so a line that reconciles here and not
    from InfluxDB alone is a timing problem, and one that fails both is not.

    Proportional, which assumes the rounding loss falls in proportion to the
    energy. It need not, which is why this is a diagnostic and not a default.
    A direction the shape has no energy in is left at zero rather than invented.
    """
    shape_import, shape_export = totals(shape)
    target_import, target_export = totals(totals_from)
    scale_import = target_import / shape_import if shape_import else 0.0
    scale_export = target_export / shape_export if shape_export else 0.0
    return [
        replace(r, imported=r.imported * scale_import, exported=r.exported * scale_export)
        for r in shape
    ]


def rounding_shortfall(low: float, high: float, intervals: int) -> bool:
    """Whether ``low`` trails ``high`` by no more than two-decimal rounding can lose.

    Bounded, because an unbounded allowance would excuse a missing day as
    readily as a rounding error -- and a missing day is what the comparison
    exists to catch.
    """
    return 0.0 <= high - low <= ROUNDING_PER_INTERVAL * intervals


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
        # Two-decimal rounding can only lose so much: at most half a hundredth
        # per interval, so 2,880 quarter-hours caps out near 14 kWh. Bounding
        # the exemption matters -- unbounded, "Green Button reads low" would
        # excuse a shortfall of any size, including a genuinely missing day,
        # which is the exact failure this comparison exists to catch.
        #
        # Either way round. When the export is what the bill was priced from,
        # its shortfall shows up as the *other* source reading a little high,
        # and that is the same rounding seen from the other side.
        note = ""
        expected = False
        if name == GREEN_BUTTON:
            expected = rounding_shortfall(other_import, base_import, len(series))
            note = (
                "Green Button rounds each interval to two decimals, so reading "
                "a little low is the export's own behaviour"
            )
        elif primary == GREEN_BUTTON:
            expected = rounding_shortfall(base_import, other_import, len(readings[primary]))
            note = (
                "Green Button, which priced this bill, rounds each interval to two "
                "decimals, so another source reading a little high is the export's "
                "own behaviour"
            )
        deltas.append(
            SourceDelta(
                left=name,
                right=primary,
                imported_delta=other_import - base_import,
                exported_delta=other_export - base_export,
                note=note if expected else "",
                significant=not expected and not allowed.kwh_ok(other_import, base_import),
            )
        )

    if classify is not None:
        base_peak = peak_share(readings[primary], classify)

        for name, series in readings.items():
            if name == primary:
                continue
            other_peak = peak_share(series, classify)
            # Judged against the same allowance as a total, because the money
            # rides on the difference between the two rates, not on the size of
            # either bucket.
            deltas.append(
                SourceDelta(
                    left=f"{name} peak",
                    right=f"{primary} peak",
                    imported_delta=other_peak - base_peak,
                    exported_delta=0.0,
                    note="which hours the energy arrived in, where the two rates differ",
                    significant=not allowed.kwh_ok(other_peak, base_peak),
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
        # Exports too, where the statement bills them. Before this the check
        # read imports alone, so a meter that lost or invented export -- the
        # side every credit is priced from -- passed it.
        exported = statement.billed_export_kwh
        exported_delta = 0.0 if exported is None else exported - base_export

        # The statement totals unrounded reads, so when the bill was priced
        # from the rounded export the statement reads a little *high* -- the
        # export's shortfall again, not a disagreement about what was billed.
        intervals = len(readings[primary])
        rounded = primary == GREEN_BUTTON

        def agrees(printed: float, measured: float) -> bool:
            if allowed.kwh_ok(printed, measured):
                return True
            return rounded and rounding_shortfall(measured, printed, intervals)

        note = (
            "what the utility says it billed"
            if whole_cycle
            else f"usage printed for one of {statement.service_agreements} service "
            f"agreements, so it does not describe the whole cycle"
        )
        if rounded:
            note += "; Green Button rounds each interval, so it reads a little low"
        deltas.append(
            SourceDelta(
                left="statement",
                right=primary,
                imported_delta=statement.billed_kwh - base_import,
                exported_delta=exported_delta,
                note=note,
                significant=whole_cycle
                and not (
                    agrees(statement.billed_kwh, base_import)
                    and (exported is None or agrees(exported, base_export))
                ),
            )
        )
    return deltas
