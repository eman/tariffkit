"""Adapters that publish rates in shapes other energy systems already read.

Each submodule targets one consumer's documented format. They are pure functions
over a :class:`~nem_rates.models.PriceCurve` so both the Home Assistant component
and the MQTT publisher can share them, and so they are testable without either.
"""

from .emhass import forecast_lists, forecast_payload
from .predbat import CENTS_PER_DOLLAR, raw_attributes
from .predbat import payload as predbat_payload
from .slots import local_day_window, resample

__all__ = [
    "CENTS_PER_DOLLAR",
    "forecast_lists",
    "forecast_payload",
    "local_day_window",
    "predbat_payload",
    "raw_attributes",
    "resample",
]
