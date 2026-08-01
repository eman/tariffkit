"""Real-time and forecast electricity import/export prices for PG&E E-ELEC under NEM 3.0.

    >>> from nem_rates import RateEngine
    >>> engine = RateEngine()
    >>> point = engine.price_now()
    >>> point.import_price.total, point.export_price.total  # doctest: +SKIP
    (0.33358, 0.07035)

Both sides come from published static tables, so lookups need no network access
and ``forecast`` reads ahead in a real schedule rather than predicting one.
"""

from __future__ import annotations

from .config import CcaConfig, Config
from .engine import RateEngine
from .errors import ConfigError, DataError, NemRatesError, OutOfRangeError
from .models import (
    ExportPrice,
    ImportPrice,
    PriceCurve,
    PricePoint,
    Season,
    Supplier,
    TouPeriod,
)
from .timeutil import PACIFIC, DayType, now_pacific

__version__ = "0.1.0"

__all__ = [
    "PACIFIC",
    "CcaConfig",
    "Config",
    "ConfigError",
    "DataError",
    "DayType",
    "ExportPrice",
    "ImportPrice",
    "NemRatesError",
    "OutOfRangeError",
    "PriceCurve",
    "PricePoint",
    "RateEngine",
    "Season",
    "Supplier",
    "TouPeriod",
    "__version__",
    "now_pacific",
]
