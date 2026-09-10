"""Credentials stored in the operating system's keyring."""

from __future__ import annotations

import importlib
import os
from typing import Final, Protocol, cast

from .errors import ConfigError

SERVICE: Final = "tariffkit"
SECRET_NAMES: Final = (
    "home_assistant.token",
    "influxdb.token",
    "mqtt.password",
    "mqtt.username",
    "pge.account_urn",
    "pge.browser_cookie",
    "pge.password",
    "pge.username",
    "pge.validation_cookie",
)


class _KeyringErrors(Protocol):
    KeyringError: type[Exception]
    NoKeyringError: type[Exception]


class _Keyring(Protocol):
    errors: _KeyringErrors

    def get_password(self, service: str, name: str) -> str | None: ...

    def set_password(self, service: str, name: str, value: str) -> None: ...

    def delete_password(self, service: str, name: str) -> None: ...


def _keyring() -> _Keyring | None:
    try:
        module = importlib.import_module("keyring")
    except ImportError:
        return None
    return cast(_Keyring, module)


def get_secret(name: str) -> str | None:
    """Return a named secret, or ``None`` when keyring support is not installed."""
    _validate_name(name)
    if os.environ.get("TARIFFKIT_DISABLE_KEYRING") == "1":
        return None
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except keyring.errors.NoKeyringError:
        # Headless containers commonly have the package but no OS secret
        # service. That means "no keyring source", not a failed credential read;
        # environment injection remains available there.
        return None
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"could not read {name!r} from the operating-system keyring") from exc


def set_secret(name: str, value: str) -> None:
    """Store a non-empty secret without exposing it in process arguments."""
    _validate_name(name)
    if not value:
        raise ConfigError("secret value must not be empty")
    keyring = _require_keyring()
    try:
        keyring.set_password(SERVICE, name, value)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"could not store {name!r} in the operating-system keyring") from exc


def delete_secret(name: str) -> None:
    """Delete a named secret, raising when the keyring operation fails."""
    _validate_name(name)
    keyring = _require_keyring()
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"could not delete {name!r} from the operating-system keyring") from exc


def configured_secrets() -> tuple[str, ...]:
    """Names that are present, without ever returning their values."""
    return tuple(name for name in SECRET_NAMES if get_secret(name) is not None)


def _require_keyring() -> _Keyring:
    keyring = _keyring()
    if keyring is None:
        raise ConfigError(
            "credential storage requires the 'secrets' extra: pip install 'tariffkit[secrets]'"
        )
    return keyring


def _validate_name(name: str) -> None:
    if name not in SECRET_NAMES:
        raise ConfigError(f"unknown secret {name!r}; choose one of {', '.join(SECRET_NAMES)}")
