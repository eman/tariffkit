"""Interval-data sources.

Everything here turns some external record of metered energy into
``IntervalReading`` objects for :mod:`nem_rates.billing`. Sources may pull in
dependencies -- the Home Assistant one needs a WebSocket client -- which is why
they live outside the billing package, which stays stdlib-only.

CSV is not here: it has no dependency to isolate, and it ships with billing.
"""

from .homeassistant import (
    DEFAULT_EXPORT_ENTITY,
    DEFAULT_IMPORT_ENTITY,
    HaSettings,
    describe_resolution,
    load_dotenv,
    read_statistics,
    read_statistics_async,
)

__all__ = [
    "DEFAULT_EXPORT_ENTITY",
    "DEFAULT_IMPORT_ENTITY",
    "HaSettings",
    "describe_resolution",
    "load_dotenv",
    "read_statistics",
    "read_statistics_async",
]
