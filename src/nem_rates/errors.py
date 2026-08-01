"""Exception hierarchy for nem_rates."""

from __future__ import annotations


class NemRatesError(Exception):
    """Base class for every error this package raises."""


class ConfigError(NemRatesError):
    """The supplied configuration is invalid or internally inconsistent."""


class DataError(NemRatesError):
    """Vendored rate data is missing, malformed, or does not cover the request."""


class OutOfRangeError(DataError):
    """The requested timestamp falls outside the vendored data's coverage."""
