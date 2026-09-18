"""Where credentials come from: the OS keyring, and ``.env``."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Final, Protocol, cast

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

#: The environment variable each stored secret defers to.
#:
#: Every source reads its environment (and ``.env``) before falling back to the
#: keyring, so a variable set here is where the value actually comes from.
#: Collected in one place because "nothing is in the keyring" and "nothing is
#: configured" are different answers, and a listing that cannot tell them apart
#: reads as a broken command.
SECRET_ENV: Final = {
    "home_assistant.token": "HA_TOKEN",
    "influxdb.token": "INFLUXDB3_AUTH_TOKEN",
    "mqtt.password": "TARIFFKIT_MQTT_PASSWORD",
    "mqtt.username": "TARIFFKIT_MQTT_USERNAME",
    "pge.account_urn": "PGE_ACCOUNT_URN",
    "pge.browser_cookie": "PGE_BROWSER_COOKIE",
    "pge.password": "PGE_PASSWORD",
    "pge.username": "PGE_USERNAME",
    "pge.validation_cookie": "PGE_VALIDATION_COOKIE",
}


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Parse a ``.env`` file leniently, returning what it defines.

    Here rather than beside a source client: every settings loader reads the
    same file, and it lived in the Home Assistant module only because that was
    the first one to need it -- which meant InfluxDB, PG&E and the MQTT
    publisher all imported Home Assistant to parse a text file.

    Tolerates ``KEY = "value"`` with spaces around the equals and quotes around
    the value, which is how these files are usually written by hand. Missing
    files yield nothing rather than raising: a token may equally come from the
    environment.
    """
    if path is None:
        from .config import default_dotenv_path

        path = default_dotenv_path()
    found: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return found
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def set_dotenv_secret(name: str, value: str, path: str | Path | None = None) -> Path:
    """Write a secret into the ``.env`` file as its environment variable.

    For machines with no keyring backend: the file is plain text, so it is kept
    owner-only.
    """
    _validate_name(name)
    if not value:
        raise ConfigError("secret value must not be empty")
    return set_dotenv_value(SECRET_ENV[name], value, path)


def set_dotenv_value(variable: str, value: str, path: str | Path | None = None) -> Path:
    """Set one variable in the ``.env`` file, keeping every other line as written.

    An existing line for the variable is replaced where it stands. The file is
    created owner-only, since it may hold credentials.
    """
    # `load_dotenv` strips surrounding whitespace and quotes and reads one line
    # per entry, so a value it cannot read back unchanged is refused here.
    if "\n" in value or "\r" in value or value != value.strip().strip('"').strip("'"):
        raise ConfigError(
            f"{variable} cannot be stored in .env: it has a line break, or "
            "leading or trailing whitespace or quotes"
        )
    file = _dotenv_path(path)
    lines = file.read_text(encoding="utf-8").splitlines() if file.is_file() else []
    entry = f"{variable}={value}"
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == variable:
            lines[index] = entry
            break
    else:
        lines.append(entry)
    _write_private(file, lines)
    return file


def delete_dotenv_secret(name: str, path: str | Path | None = None) -> Path:
    """Remove a secret's variable from the ``.env`` file."""
    _validate_name(name)
    variable = SECRET_ENV[name]
    file = _dotenv_path(path)
    lines = _dotenv_lines_without(file, variable)
    if not file.is_file() or len(lines) == len(file.read_text(encoding="utf-8").splitlines()):
        raise ConfigError(f"{variable} is not set in {file}")
    _write_private(file, lines)
    return file


def _dotenv_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    from .config import default_dotenv_path

    return default_dotenv_path()


def _dotenv_lines_without(file: Path, variable: str) -> list[str]:
    if not file.is_file():
        return []
    return [
        line
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.split("=", 1)[0].strip() != variable
    ]


def _write_private(file: Path, lines: list[str]) -> None:
    file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))
    file.chmod(0o600)


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


def set_secret(name: str, value: str) -> Path | None:
    """Store a non-empty secret without exposing it in process arguments.

    Goes to the OS keyring. A machine without one -- no ``keyring`` package, no
    backend (a headless box with no Secret Service), or the keyring turned off
    with ``TARIFFKIT_DISABLE_KEYRING`` -- gets the owner-only ``.env`` instead,
    which every source already reads. Returns that file when it was used, or
    ``None`` for the keyring.
    """
    _validate_name(name)
    if not value:
        raise ConfigError("secret value must not be empty")
    keyring = _available_keyring()
    if keyring is None:
        return set_dotenv_secret(name, value)
    try:
        keyring.set_password(SERVICE, name, value)
    except keyring.errors.NoKeyringError:
        return set_dotenv_secret(name, value)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"could not store {name!r} in the operating-system keyring") from exc
    return None


def delete_secret(name: str) -> Path | None:
    """Delete a named secret from wherever `set_secret` would have put it."""
    _validate_name(name)
    keyring = _available_keyring()
    if keyring is None:
        return delete_dotenv_secret(name)
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.NoKeyringError:
        return delete_dotenv_secret(name)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(f"could not delete {name!r} from the operating-system keyring") from exc
    return None


def _available_keyring() -> _Keyring | None:
    if os.environ.get("TARIFFKIT_DISABLE_KEYRING") == "1":
        return None
    return _keyring()


def configured_secrets() -> tuple[str, ...]:
    """Names that are present, without ever returning their values."""
    return tuple(name for name in SECRET_NAMES if get_secret(name) is not None)


def keyring_backend() -> str | None:
    """Which OS keyring is in use, or ``None`` when there is nothing to read.

    ``None`` covers all three ways there is no keyring: the extra is not
    installed, it is switched off, or the package resolved its "fail" backend
    because the machine offers no secret service -- the headless-container
    case. Reporting which one is in use is the only way a listing can say that
    an empty result means "nothing stored here" rather than "not looking".
    """
    if os.environ.get("TARIFFKIT_DISABLE_KEYRING") == "1":
        return None
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        backend = type(cast(Any, keyring).get_keyring())
    except Exception:  # pragma: no cover - a broken backend is not a listing error
        return None
    if backend.__module__.endswith(".fail"):
        return None
    return f"{backend.__module__}.{backend.__qualname__}"


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
