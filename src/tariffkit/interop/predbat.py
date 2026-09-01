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
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal, Protocol, TypedDict

from ..components import ComponentGroup
from ..models import PriceCurve
from ..timeutil import now_pacific, to_pacific
from .slots import local_day_window, resample

#: Predbat assumes pence; cents keep the same order of magnitude.
CENTS_PER_DOLLAR = 100.0

Direction = Literal["import", "export"]


PredbatRate = TypedDict("PredbatRate", {"from": str, "to": str, "rate": float})
PredbatAttributes = dict[str, list[PredbatRate]]
PredbatPayload = dict[str, PredbatAttributes]


class ForecastEngine(Protocol):
    """The small engine surface needed to build Predbat's two-day payload."""

    def forecast(self, hours: int, start: datetime | None = None) -> PriceCurve: ...


def raw_attributes(
    curve: PriceCurve,
    *,
    direction: Direction,
    minutes: int = 30,
    scale: float = CENTS_PER_DOLLAR,
    today: date | None = None,
    split: bool = True,
) -> PredbatAttributes:
    """Build the ``raw_today`` / ``raw_tomorrow`` pair for one direction.

    Partition is by Pacific calendar date rather than by offset from now, which is
    what Predbat means by "today". Slots beyond tomorrow are dropped. A horizon too
    short to reach tomorrow leaves ``raw_tomorrow`` empty, which is how Predbat
    already represents "tomorrow's rates are not published yet".

    ``split`` additionally emits a ``_generation`` and a ``_non_generation`` series
    per day, so a Home Assistant dashboard can draw the price as two stacked bands.
    The pair is exact rather than approximate: the non-generation rate is the
    difference of the two *rounded* values, so the bands always re-sum to the plain
    series slot for slot. Predbat itself reads only ``raw_today`` / ``raw_tomorrow``
    and ignores the rest, so the extra series cost it nothing.

    The second band is deliberately not called ``delivery``. It is everything that
    is not generation -- on import that is distribution, transmission, surcharges
    and credits; on export it is the delivery component *plus* the ACC Plus and
    CARE/FERA credits -- whereas :class:`~tariffkit.components.ComponentGroup`
    already uses ``delivery`` for the narrower export-side band published under
    ``groups``. Two different numbers under one name in one payload is a trap.
    """
    anchor = today if today is not None else now_pacific().date()
    tomorrow = anchor + timedelta(days=1)
    buckets: dict[str, list[PredbatRate]] = {"raw_today": [], "raw_tomorrow": []}
    if split:
        buckets |= {
            "raw_today_generation": [],
            "raw_tomorrow_generation": [],
            "raw_today_non_generation": [],
            "raw_tomorrow_non_generation": [],
        }

    for slot in resample(curve, minutes):
        day = slot.start.date()
        if day == anchor:
            key = "raw_today"
        elif day == tomorrow:
            key = "raw_tomorrow"
        else:
            continue
        price = slot.import_price if direction == "import" else slot.export_price

        start_iso = slot.start.isoformat()
        end_iso = slot.end.isoformat()
        rate = round(price.total * scale, 5)
        buckets[key].append({"from": start_iso, "to": end_iso, "rate": rate})
        if not split:
            continue

        # grouped() is the one definition of what counts as generation, and it
        # already folds an unrecognized component into OTHER rather than dropping
        # it, so the groups sum back to the total.
        generation = round(price.grouped()[ComponentGroup.GENERATION] * scale, 5)
        buckets[f"{key}_generation"].append({"from": start_iso, "to": end_iso, "rate": generation})
        buckets[f"{key}_non_generation"].append(
            {"from": start_iso, "to": end_iso, "rate": round(rate - generation, 5)}
        )

    return buckets


def payload(
    engine: ForecastEngine,
    moment: datetime | None = None,
    *,
    minutes: int = 30,
    scale: float = CENTS_PER_DOLLAR,
    split: bool = True,
) -> PredbatPayload:
    """Both directions, anchored to local midnight.

    Takes an engine rather than a curve on purpose. A forecast starting at the
    current hour would give Predbat a ``raw_today`` truncated at, say, 18:00, and
    Predbat backfills a short day by copying the same slots from 24 hours earlier
    -- plausible for a flat-ish agile tariff, wrong for an export curve this
    day-shaped. Anchoring to midnight makes both days complete by construction.

    ``split`` is passed through to :func:`raw_attributes`. Turn it off for a
    consumer that cannot afford the extra series -- see the MQTT publisher, whose
    attribute topics have no way to opt out of Home Assistant's recorder.

    Cheap to call: every lookup is an O(1) index into vendored tables, no I/O.
    """
    anchor = to_pacific(moment) if moment else now_pacific()
    start, hours = local_day_window(anchor, days=2)
    curve = engine.forecast(hours, start=start)
    today = anchor.date()
    return {
        "import": raw_attributes(
            curve, direction="import", minutes=minutes, scale=scale, today=today, split=split
        ),
        "export": raw_attributes(
            curve, direction="export", minutes=minutes, scale=scale, today=today, split=split
        ),
    }
