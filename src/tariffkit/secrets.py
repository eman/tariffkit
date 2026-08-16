"""Credentials stored in the operating system's keyring."""

from __future__ import annotations

import importlib
import os
import re
from typing import Final, Protocol, cast

from .errors import ConfigError

SERVICE: Final = "tariffkit"
_CREDENTIAL_SET = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,62}[a-z0-9])?$")
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


def get_named_secret(credential_set: str, name: str) -> str | None:
    """Read a secret from a named keyring set without exposing its value."""
    _validate_credential_set(credential_set)
    _validate_name(name)
    if os.environ.get("TARIFFKIT_DISABLE_KEYRING") == "1":
        return None
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(f"{SERVICE}:credential:{credential_set}", name)
    except keyring.errors.NoKeyringError:
        return None
    except keyring.errors.KeyringError as exc:
        raise ConfigError(
            f"could not read {name!r} from credential set {credential_set!r}"
        ) from exc


def set_named_secret(credential_set: str, name: str, value: str) -> None:
    """Store a secret in a named keyring set."""
    _validate_credential_set(credential_set)
    _validate_name(name)
    if not value:
        raise ConfigError("secret value must not be empty")
    keyring = _require_keyring()
    try:
        keyring.set_password(f"{SERVICE}:credential:{credential_set}", name, value)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"could not store {name!r} in credential set {credential_set!r}") from exc


def delete_named_secret(credential_set: str, name: str) -> None:
    """Delete a secret from a named keyring set."""
    _validate_credential_set(credential_set)
    _validate_name(name)
    keyring = _require_keyring()
    try:
        keyring.delete_password(f"{SERVICE}:credential:{credential_set}", name)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(
            f"could not delete {name!r} from credential set {credential_set!r}"
        ) from exc


def configured_named_secrets(credential_set: str) -> tuple[str, ...]:
    """List names present in a named credential set, without values."""
    return tuple(
        name for name in SECRET_NAMES if get_named_secret(credential_set, name) is not None
    )


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


def _validate_credential_set(name: str) -> None:
    if _CREDENTIAL_SET.fullmatch(name) is None:
        raise ConfigError("credential set must be a lowercase safe name")
