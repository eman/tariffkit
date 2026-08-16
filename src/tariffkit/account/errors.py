"""Errors raised by account profiles and their managed storage."""

from __future__ import annotations

from ..errors import TariffKitError


class AccountError(TariffKitError):
    """An account profile is invalid or cannot price the requested date."""


class ProfileNotFoundError(AccountError):
    """A named profile does not exist."""


class ProfileNameError(AccountError):
    """A named profile identifier is not a safe slug."""


class ProfileStorageError(AccountError):
    """A managed profile file is malformed or unsafe to use."""


class ProfileConflictError(ProfileStorageError):
    """A profile changed after the caller read the revision being replaced."""
