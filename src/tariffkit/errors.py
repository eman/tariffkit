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


class PublishError(TariffKitError):
    """A message could not be handed to the broker.

    Everything published is retained, so a dropped message leaves the previous
    value being served as though it were current.
    """
