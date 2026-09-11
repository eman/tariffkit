"""Errors raised by account profiles."""

from __future__ import annotations

from ..errors import TariffKitError


class AccountError(TariffKitError):
    """An account profile is invalid or cannot price the requested date."""
