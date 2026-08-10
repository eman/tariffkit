"""EMHASS-shaped cost forecasts.

EMHASS takes the tariff as ``load_cost_forecast`` and the export compensation as
``prod_price_forecast``, passed as runtime parameters to ``/action/dayahead-optim``
or ``/action/naive-mpc-optim``.

Both are **bare lists**, positional against EMHASS's own forecast timestamps --
its ``method='list'`` path reads them straight into a DataFrame column and only
checks the length. So the first value must line up with the slot EMHASS is
currently in, which is what ``since`` is for.

Values stay in dollars per kWh. EMHASS has no currency assumption of its own; it
optimises against whatever scale the costs arrive in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import PriceCurve, PricePoint
from .slots import resample

#: EMHASS's shipped ``optimization_time_step``. Its docs mention 60 in places,
#: but config_defaults.json ships 30, and a list shorter than the horizon is
#: rejected outright -- so match the config, not the prose.
DEFAULT_MINUTES = 30


def _aligned(curve: PriceCurve, minutes: int, since: datetime | None) -> tuple[PricePoint, ...]:
    """Resampled slots, dropping any that have already elapsed.

    The engine floors to the hour, so at 10:45 a curve starts at 10:00 while
    EMHASS's own timeline starts at 10:30. Without trimming, every value would
    be shifted one slot early.
    """
    slots = resample(curve, minutes)
    if since is None:
        return slots
    return tuple(slot for slot in slots if slot.end > since)


def forecast_lists(
    curve: PriceCurve,
    *,
    minutes: int = DEFAULT_MINUTES,
    since: datetime | None = None,
) -> dict[str, Any]:
    """The runtime-parameter form: two bare lists plus their length.

    ``prediction_horizon`` is included because EMHASS needs it for
    ``naive-mpc-optim`` and it must agree with the list lengths.
    """
    slots = _aligned(curve, minutes, since)
    return {
        "load_cost_forecast": [round(s.import_price.total, 6) for s in slots],
        "prod_price_forecast": [round(s.export_price.total, 6) for s in slots],
        "prediction_horizon": len(slots),
    }


def forecast_payload(
    curve: PriceCurve,
    *,
    minutes: int = DEFAULT_MINUTES,
    since: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """Timestamped form, keyed by ISO 8601 start.

    Not what the documented ``rest_command`` uses -- EMHASS's list path expects a
    sequence, and a mapping is not known to be accepted. Kept because it is the
    self-describing way to hand these series to anything else, and because the
    keys stay distinct across the fall-back day's repeated hour.
    """
    slots = _aligned(curve, minutes, since)
    return {
        "load_cost_forecast": {s.start.isoformat(): round(s.import_price.total, 6) for s in slots},
        "prod_price_forecast": {s.start.isoformat(): round(s.export_price.total, 6) for s in slots},
    }
