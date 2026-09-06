"""Predbat-shaped rate attributes.

Predbat reads its rates off whichever Home Assistant entity ``apps.yaml`` points
``metric_octopus_import`` / ``metric_octopus_export`` at, expecting two attributes
-- ``raw_today`` and ``raw_tomorrow`` -- each a list of ``{from, to, rate}``.

Two adaptations are needed:

* **Shape and scale.** Predbat treats ``start`` / ``end`` / ``value`` entries as
  currency-unit values and multiplies them by 100. The ``from`` / ``to`` / ``rate``
  form is already denominated in pence, so cents can be supplied unchanged and keep
  the same useful magnitude. Predbat's display will label them ``p``.
* **Slot length.** Predbat plans in 30-minute slots aligned to :00 and :30 by
  default, so the hourly curve is resampled before partitioning.

The same two-day partition is reused for the per-group dashboard curves in
:func:`group_attributes`. They are not for Predbat -- it reads only the totals --
but the day boundaries mean the same thing for both, and a chart that stacks a
band against the price wants the two to line up slot for slot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Literal, Protocol, TypedDict

from ..components import EXPORT_GROUPS, IMPORT_GROUPS, ComponentGroup
from ..models import PriceCurve, PricePoint
from ..timeutil import now_pacific, to_pacific
from .slots import local_day_window, resample

#: Predbat assumes pence; cents keep the same order of magnitude.
CENTS_PER_DOLLAR = 100.0

Direction = Literal["import", "export"]


PredbatRate = TypedDict("PredbatRate", {"from": str, "to": str, "rate": float})
PredbatAttributes = dict[str, list[PredbatRate]]
PredbatPayload = dict[str, PredbatAttributes]
#: One two-day curve per direction and component group.
GroupPayload = dict[str, dict[ComponentGroup, PredbatAttributes]]


class ForecastEngine(Protocol):
    """The small engine surface needed to build Predbat's two-day payload."""

    def forecast(self, hours: int, start: datetime | None = None) -> PriceCurve: ...


def _day_buckets(
    curve: PriceCurve,
    minutes: int,
    today: date | None,
    rate_of: Callable[[PricePoint], float],
) -> PredbatAttributes:
    """Partition ``curve`` into today and tomorrow, valuing each slot with ``rate_of``.

    Partition is by Pacific calendar date rather than by offset from now, which is
    what Predbat means by "today". Slots beyond tomorrow are dropped. A horizon too
    short to reach tomorrow leaves ``raw_tomorrow`` empty, which is how Predbat
    already represents "tomorrow's rates are not published yet".
    """
    anchor = today if today is not None else now_pacific().date()
    tomorrow = anchor + timedelta(days=1)
    buckets: dict[str, list[PredbatRate]] = {"raw_today": [], "raw_tomorrow": []}

    for slot in resample(curve, minutes):
        day = slot.start.date()
        if day == anchor:
            key = "raw_today"
        elif day == tomorrow:
            key = "raw_tomorrow"
        else:
            continue
        buckets[key].append(
            {
                "from": slot.start.isoformat(),
                "to": slot.end.isoformat(),
                "rate": rate_of(slot),
            }
        )

    return buckets


def raw_attributes(
    curve: PriceCurve,
    *,
    direction: Direction,
    minutes: int = 30,
    scale: float = CENTS_PER_DOLLAR,
    today: date | None = None,
) -> PredbatAttributes:
    """Build the ``raw_today`` / ``raw_tomorrow`` pair for one direction."""

    def rate_of(slot: PricePoint) -> float:
        price = slot.import_price if direction == "import" else slot.export_price
        return round(price.total * scale, 5)

    return _day_buckets(curve, minutes, today, rate_of)


def group_attributes(
    curve: PriceCurve,
    *,
    direction: Direction,
    group: ComponentGroup,
    minutes: int = 30,
    scale: float = CENTS_PER_DOLLAR,
    today: date | None = None,
) -> PredbatAttributes:
    """The same two days, restricted to one component group.

    One curve per band, rather than a headline band and a residual. Which split a
    dashboard wants is the dashboard's business: generation against everything
    else, the bill's Delivery line against generation, or all of the bands
    stacked. Naming one of them in the payload would pick for the reader, and the
    leftover would mean something different on import than on export.

    ``grouped()`` is the one definition of what lands where, and it folds an
    unrecognized component into ``OTHER`` rather than dropping it, so a
    direction's groups re-sum to the series that :func:`raw_attributes` publishes.
    """

    def rate_of(slot: PricePoint) -> float:
        price = slot.import_price if direction == "import" else slot.export_price
        return round(price.grouped()[group] * scale, 5)

    return _day_buckets(curve, minutes, today, rate_of)


def payload(
    engine: ForecastEngine,
    moment: datetime | None = None,
    *,
    minutes: int = 30,
    scale: float = CENTS_PER_DOLLAR,
) -> PredbatPayload:
    """Both directions, anchored to local midnight.

    Takes an engine rather than a curve on purpose. A forecast starting at the
    current hour would give Predbat a ``raw_today`` truncated at, say, 18:00, and
    Predbat backfills a short day by copying the same slots from 24 hours earlier
    -- plausible for a flat-ish agile tariff, wrong for an export curve this
    day-shaped. Anchoring to midnight makes both days complete by construction.

    Cheap to call: every lookup is an O(1) index into vendored tables, no I/O.
    """
    anchor = to_pacific(moment) if moment else now_pacific()
    start, hours = local_day_window(anchor, days=2)
    curve = engine.forecast(hours, start=start)
    today = anchor.date()
    return {
        "import": raw_attributes(
            curve, direction="import", minutes=minutes, scale=scale, today=today
        ),
        "export": raw_attributes(
            curve, direction="export", minutes=minutes, scale=scale, today=today
        ),
    }


def group_payload(
    engine: ForecastEngine,
    moment: datetime | None = None,
    *,
    minutes: int = 30,
    scale: float = CENTS_PER_DOLLAR,
) -> GroupPayload:
    """Every band of both directions, anchored to local midnight like :func:`payload`.

    Each band is published on its own entity and its own MQTT topic rather than
    all of them in one attribute blob, which keeps every payload well inside Home
    Assistant's recorder ceiling and lets a chart subscribe to just the bands it
    draws.
    """
    anchor = to_pacific(moment) if moment else now_pacific()
    start, hours = local_day_window(anchor, days=2)
    curve = engine.forecast(hours, start=start)
    today = anchor.date()
    directions: tuple[tuple[Direction, tuple[ComponentGroup, ...]], ...] = (
        ("import", IMPORT_GROUPS),
        ("export", EXPORT_GROUPS),
    )
    return {
        direction: {
            group: group_attributes(
                curve,
                direction=direction,
                group=group,
                minutes=minutes,
                scale=scale,
                today=today,
            )
            for group in groups
        }
        for direction, groups in directions
    }
