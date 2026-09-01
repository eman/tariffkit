"""Adapters that publish rates in shapes other energy systems already read.

Each submodule targets one consumer's documented format. They are pure functions
over a :class:`~tariffkit.models.PriceCurve` so both the Home Assistant component
and the MQTT publisher can share them, decoupling them from specific integrations.
"""

from .emhass import forecast_lists, forecast_payload
from .predbat import CENTS_PER_DOLLAR, group_attributes, raw_attributes
from .predbat import group_payload as predbat_group_payload
from .predbat import payload as predbat_payload
from .slots import local_day_window, resample

__all__ = [
    "CENTS_PER_DOLLAR",
    "forecast_lists",
    "forecast_payload",
    "group_attributes",
    "local_day_window",
    "predbat_group_payload",
    "predbat_payload",
    "raw_attributes",
    "resample",
]
