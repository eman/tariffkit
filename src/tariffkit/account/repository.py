"""Private, versioned JSON storage for named account profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from ..config import default_config_path
from ..errors import TariffKitError
from .errors import (
    ProfileConflictError,
    ProfileNameError,
    ProfileNotFoundError,
    ProfileStorageError,
)
from .model import AccountProfile

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_MODE_DIR = 0o700
_MODE_FILE = 0o600


def validate_profile_name(name: str) -> str:
    """Validate a profile slug before it participates in path construction."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ProfileNameError("profile name must be a lowercase slug")
    return name


def configured_profile_name(config_path: str | Path | None = None) -> str | None:
    """Return the configured default profile without loading any credentials.

    Profile selection is deliberately kept separate from ``Config`` parsing:
    the main TOML file may contain integration sections that are not pricing
    fields, while a stateless ``Config`` must continue to reject unknown
    pricing keys.
    """
    for environment_name in ("TARIFFKIT_ACCOUNT", "TARIFFKIT_PROFILE"):
        if value := os.environ.get(environment_name):
            return validate_profile_name(value)

    path = Path(config_path) if config_path is not None else default_config_path()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            table = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileStorageError(f"could not read profile selection from {path}") from exc

    account = table.get("account")
    candidates: list[object] = []
    if isinstance(account, Mapping):
        candidates.extend(account.get(key) for key in ("default_profile", "profile", "default"))
    elif account is not None:
        candidates.append(account)
    candidates.extend(table.get(key) for key in ("default_profile", "profile", "account_profile"))
    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, str):
            raise ProfileStorageError("configured profile name must be a string")
        return validate_profile_name(candidate)
    return None


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value) if value else Path.home() / ".config"


def _json_bytes(profile: AccountProfile, *, name: str) -> bytes:
    if profile.name not in ("", name):
        raise ProfileStorageError(f"profile is named {profile.name!r}, not the requested {name!r}")
    stored = (
        profile
        if profile.name == name
        else AccountProfile(
            epochs=profile.epochs,
            name=name,
            credential_set=profile.credential_set,
            observations=profile.observations,
            meter_sources=profile.meter_sources,
        )
    )
    try:
        payload = (
            json.dumps(
                stored.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ProfileStorageError("profile contains values that are not JSON-compatible") from exc
    return payload


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class NamedProfileRepository:
    """Manage user-only profile files below the XDG configuration directory."""

    def __init__(self, config_home: str | Path | None = None) -> None:
        base = Path(config_home) if config_home is not None else _config_home()
        self.directory = base / "tariffkit" / "accounts"
        self._check_no_symlink_components(base)
        self._ensure_directory()

    def _check_no_symlink(self, path: Path) -> None:
        if path.is_symlink():
            raise ProfileStorageError(f"refusing symlink in profile path: {path}")

    def _check_no_symlink_components(self, path: Path) -> None:
        """Reject an escaping symlink anywhere below the configured root."""
        current = Path(path.anchor) if path.is_absolute() else Path()
        for part in path.parts:
            if path.is_absolute() and part == path.anchor:
                continue
            current /= part
            self._check_no_symlink(current)

    def _ensure_directory(self) -> None:
        try:
            root = self.directory
            base = root.parent.parent
            self._check_no_symlink_components(base)
            base.mkdir(mode=_MODE_DIR, parents=True, exist_ok=True)
            base.chmod(_MODE_DIR)
            for current in (root.parent, root):
                self._check_no_symlink_components(current)
                current.mkdir(mode=_MODE_DIR, exist_ok=True)
                self._check_no_symlink_components(current)
                current.chmod(_MODE_DIR)
        except OSError as exc:
            raise ProfileStorageError(f"could not create private profile directory {root}") from exc

    def path_for(self, name: str) -> Path:
        """Return a safe profile path after validating the name."""
        validated = validate_profile_name(name)
        self._check_private_directory()
        path = self.directory / f"{validated}.json"
        self._check_no_symlink_components(path)
        return path

    def names(self) -> tuple[str, ...]:
        """List valid regular profile files, refusing unsafe entries."""
        self._check_private_directory()
        result: list[str] = []
        for path in self.directory.iterdir():
            self._check_no_symlink_components(path)
            if path.suffix != ".json" or not path.is_file():
                continue
            name = path.stem
            validate_profile_name(name)
            result.append(name)
        return tuple(sorted(result))

    def load(self, name: str) -> AccountProfile:
        path = self.path_for(name)
        if not path.exists():
            raise ProfileNotFoundError(f"profile {name!r} does not exist")
        if path.is_symlink() or not path.is_file():
            raise ProfileStorageError(f"profile path is not a regular file: {path}")
        if stat.S_IMODE(path.stat().st_mode) != _MODE_FILE:
            raise ProfileStorageError(f"profile {name!r} is not private (expected mode 0600)")
        try:
            raw = path.read_bytes()
            parsed = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value {value}")
                ),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProfileStorageError(f"profile {name!r} is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ProfileStorageError("profile JSON must contain an object")
        try:
            profile = AccountProfile.from_dict(parsed)
        except (TariffKitError, TypeError, ValueError) as exc:
            raise ProfileStorageError(f"profile {name!r} failed validation: {exc}") from exc
        if profile.name != name:
            raise ProfileStorageError(f"profile name does not match filename {name!r}")
        profile = AccountProfile(
            epochs=profile.epochs,
            name=profile.name,
            credential_set=profile.credential_set,
            observations=profile.observations,
            meter_sources=profile.meter_sources,
        )
        return _with_revision(profile, _revision(raw))

    def save(
        self,
        name: str,
        profile: AccountProfile,
        *,
        expected_revision: str | None = None,
    ) -> AccountProfile:
        """Atomically save a validated profile with optimistic concurrency."""
        path = self.path_for(name)
        replacement = _json_bytes(profile, name=name)
        original = self._read_existing(path)
        original_revision = None if original is None else _revision(original)
        expected = expected_revision
        if expected is None:
            expected = getattr(profile, "_revision", None)
        if original_revision != expected:
            raise ProfileConflictError(f"profile {name!r} changed; reload it before saving")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, _MODE_FILE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            current = self._read_existing(path)
            current_revision = None if current is None else _revision(current)
            if current_revision != original_revision:
                raise ProfileConflictError(f"profile {name!r} changed while it was being saved")
            temporary.replace(path)
            self._check_no_symlink(path)
            path.chmod(_MODE_FILE)
            self._fsync_directory()
        except ProfileConflictError:
            raise
        except OSError as exc:
            raise ProfileStorageError(f"could not save profile {name!r}") from exc
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        saved = AccountProfile(
            epochs=profile.epochs,
            name=name,
            credential_set=profile.credential_set,
            observations=profile.observations,
            meter_sources=profile.meter_sources,
        )
        return _with_revision(saved, _revision(replacement))

    def delete(self, name: str, *, expected_revision: str | None = None) -> None:
        """Delete a profile only if its revision is the one the caller read."""
        path = self.path_for(name)
        original = self._read_existing(path)
        if original is None:
            raise ProfileNotFoundError(f"profile {name!r} does not exist")
        revision = _revision(original)
        if expected_revision is not None and revision != expected_revision:
            raise ProfileConflictError(f"profile {name!r} changed; reload it before deleting")
        try:
            path.unlink()
            self._fsync_directory()
        except OSError as exc:
            raise ProfileStorageError(f"could not delete profile {name!r}") from exc

    def _read_existing(self, path: Path) -> bytes | None:
        self._check_no_symlink_components(path)
        try:
            return path.read_bytes() if path.exists() else None
        except OSError as exc:
            raise ProfileStorageError(f"could not read profile path {path}") from exc

    def _check_private_directory(self) -> None:
        self._check_no_symlink(self.directory)
        try:
            mode = stat.S_IMODE(self.directory.stat().st_mode)
        except OSError as exc:
            raise ProfileStorageError("could not inspect profile directory") from exc
        if mode != _MODE_DIR:
            raise ProfileStorageError("profile directory is not private (expected mode 0700)")

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(self.directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _with_revision(profile: AccountProfile, revision: str) -> AccountProfile:
    object.__setattr__(profile, "_revision", revision)
    return profile
