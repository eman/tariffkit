"""Exception hierarchy for tariffkit."""

from __future__ import annotations


class TariffKitError(Exception):
    """Base class for every error this package raises."""


class ConfigError(TariffKitError):
    """The supplied configuration is invalid or internally inconsistent."""


class DataError(TariffKitError):
    """Vendored rate data is missing, malformed, or does not cover the request."""


class OutOfRangeError(DataError):
    """The requested timestamp falls outside the vendored data's coverage."""
