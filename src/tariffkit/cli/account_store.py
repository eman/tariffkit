"""Private, versioned JSON storage for the account.

This is the command line's own file, not the library's. Where a machine keeps
the account, how it is locked, and who may read it are decisions an
application makes; a library that made them would be reading ``~/.config`` the
moment it was imported. Everything below the CLI is handed an
:class:`~tariffkit.account.AccountProfile` instead.

One account, in one file. There used to be a directory of named profiles and a
way to select between them, which earned its complexity only for someone billing
several service agreements under one login -- a landlord with rental units. For
everyone else it was a name to invent, a flag to remember, and a silent wrong
answer when the flag was forgotten: a bill priced from ``config.toml`` instead of
from the agreement's own history read a CCA account as bundled, which gives it
one export credit bank where it has two.

What is kept is the part that earns its keep: the dated history. A ``Config``
describes one moment and a real agreement does not, so every bill prices with
the settings in force over its own days.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from ..account import AccountError, AccountProfile
from ..config import config_home
from ..errors import TariffKitError


class ProfileNotFoundError(AccountError):
    """No account has been set up."""


class ProfileStorageError(AccountError):
    """The account file is malformed or unsafe to use."""


class ProfileConflictError(ProfileStorageError):
    """The account changed after the caller read the revision being replaced."""


_MODE_DIR = 0o700
_MODE_FILE = 0o600
_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()

#: The account's filename below the tariffkit configuration directory.
ACCOUNT_FILE = "account.json"

#: Where named profiles used to live. Read once, to adopt one automatically.
LEGACY_DIRECTORY = "accounts"


def _json_bytes(profile: AccountProfile) -> bytes:
    try:
        return (
            json.dumps(
                profile.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ProfileStorageError(
            "the account contains values that are not JSON-compatible"
        ) from exc


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class AccountStore:
    """Read and write the one account file, privately and atomically."""

    def __init__(self, base_directory: str | Path | None = None) -> None:
        """Resolve the paths. Creating anything waits until something is written.

        Constructing this used to `mkdir` and `chmod` the configuration
        directory, which made merely asking whether an account exists a write:
        every `now`, `forecast`, `bill`, `mqtt` and `serve` created and
        tightened a directory it might never read, and on a read-only mount --
        which is how the container documentation says to run `serve` -- the
        chmod failed at startup.
        """
        self.directory = (
            Path(base_directory) / "tariffkit" if base_directory is not None else config_home()
        )
        self._check_no_symlink_components(self.directory.parent)
        self.path = self.directory / ACCOUNT_FILE
        self._check_no_symlink_components(self.path)

    def _check_no_symlink(self, path: Path) -> None:
        if path.is_symlink():
            raise ProfileStorageError(f"refusing symlink in the account path: {path}")

    def _check_no_symlink_components(self, path: Path) -> None:
        self._check_no_symlink(path)
        for parent in path.parents:
            self._check_no_symlink(parent)
            if parent == parent.parent:
                break

    def _ensure_directory(self) -> None:
        """Create the directory, privately, on the way to writing in it."""
        try:
            self.directory.mkdir(mode=_MODE_DIR, parents=True, exist_ok=True)
            self._check_no_symlink_components(self.directory)
            self.directory.chmod(_MODE_DIR)
        except OSError as exc:
            # An unwritable or read-only configuration root is a thing to
            # report, not a traceback: `main` turns a TariffKitError into
            # "error: ..." and exit 1, and lets everything else through.
            raise ProfileStorageError(
                f"could not create the account directory {self.directory}: {exc}"
            ) from exc

    def _check_directory(self) -> None:
        """Refuse a path that is not a directory of ours. Mode is not checked here.

        Reading used to demand exactly 0700 and, before that, quietly chmod-ed
        its way to it -- so the check only ever passed because construction had
        just fixed it. With creation made lazy the self-heal went too, and the
        demand outlived it: a directory made by the `mkdir -p` in
        docs/accounts.md is 0755 under a default umask, and every command that
        so much as asks whether an account exists refused to run. A read-only
        0500 mount was refused for being *more* private than asked.

        What protects the account is the file's own 0600, checked on every
        read. The directory is tightened when we create or write it, which is
        the point at which doing so is ours to do.
        """
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ProfileStorageError(f"the account directory is not a directory: {self.directory}")

    def exists(self) -> bool:
        """Whether an account has been set up, adopting a legacy profile if one fits."""
        if not self.directory.exists():
            return False
        self._check_directory()
        if self.path.is_file():
            return True
        return self._adopt_legacy() is not None

    def load(self) -> AccountProfile:
        if not self.directory.exists():
            raise ProfileNotFoundError(
                "no account has been set up; run 'tariffkit account init' to create one"
            )
        self._check_directory()
        if not self.path.exists() and self._adopt_legacy() is None:
            raise ProfileNotFoundError(
                "no account has been set up; run 'tariffkit account init' to create one"
            )
        path = self.path
        if path.is_symlink() or not path.is_file():
            raise ProfileStorageError(f"the account path is not a regular file: {path}")
        if stat.S_IMODE(path.stat().st_mode) != _MODE_FILE:
            raise ProfileStorageError("the account file is not private (expected mode 0600)")
        try:
            raw = path.read_bytes()
            parsed = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value {value}")
                ),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProfileStorageError("the account file is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ProfileStorageError("the account file must contain an object")
        try:
            profile = AccountProfile.from_dict(parsed)
        except (TariffKitError, TypeError, ValueError) as exc:
            raise ProfileStorageError(f"the account failed validation: {exc}") from exc
        return _with_revision(profile, _revision(raw))

    def save(
        self,
        profile: AccountProfile,
        *,
        expected_revision: str | None = None,
    ) -> AccountProfile:
        """Atomically save a validated account with optimistic concurrency."""
        self._ensure_directory()
        self._check_directory()
        replacement = _json_bytes(profile)
        with self._account_lock():
            original = self._read_existing(self.path)
            original_revision = None if original is None else _revision(original)
            expected = expected_revision
            if expected is None:
                expected = getattr(profile, "_revision", None)
            if original_revision != expected:
                raise ProfileConflictError("the account changed on disk; reload it before saving")
            fd, temporary_name = tempfile.mkstemp(
                prefix=".account.", suffix=".tmp", dir=self.directory
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(fd, _MODE_FILE)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(replacement)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.path)
                self._check_no_symlink(self.path)
                self.path.chmod(_MODE_FILE)
                self._fsync_directory()
            except OSError as exc:
                raise ProfileStorageError("could not save the account") from exc
            finally:
                with suppress(FileNotFoundError):
                    temporary.unlink()
        return _with_revision(profile, _revision(replacement))

    def delete(self, *, expected_revision: str | None = None) -> None:
        """Delete the account only if its revision is the one the caller read."""
        if not self.directory.exists():
            raise ProfileNotFoundError("no account has been set up")
        self._check_directory()
        with self._account_lock():
            original = self._read_existing(self.path)
            if original is None:
                raise ProfileNotFoundError("no account has been set up")
            revision = _revision(original)
            if expected_revision is not None and revision != expected_revision:
                raise ProfileConflictError("the account changed; reload it before deleting")
            try:
                self.path.unlink()
                self._fsync_directory()
            except OSError as exc:
                raise ProfileStorageError("could not delete the account") from exc

    def _adopt_legacy(self) -> Path | None:
        """Move a lone named profile into place, once.

        Adopted rather than migrated on demand because the old layout allowed
        several and this one does not: picking between them is a decision, and
        guessing it would price bills from an agreement the owner did not
        choose. One profile is unambiguous, so it is taken; several are left
        alone for the owner to resolve, and ``load`` then reports that no
        account is set up rather than inventing one.
        """
        legacy = self.directory / LEGACY_DIRECTORY
        if not legacy.is_dir() or legacy.is_symlink():
            return None
        found = sorted(p for p in legacy.glob("*.json") if p.is_file() and not p.is_symlink())
        if len(found) != 1:
            return None
        temporary: Path | None = None
        try:
            raw = found[0].read_bytes()
            fd, temporary_name = tempfile.mkstemp(
                prefix=".account.", suffix=".tmp", dir=self.directory
            )
            temporary = Path(temporary_name)
            os.fchmod(fd, _MODE_FILE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            self.path.chmod(_MODE_FILE)
            self._fsync_directory()
            return self.path
        except OSError:
            # Adoption is a convenience; failing it is not worth failing a
            # command over. The temporary goes either way, or every retry
            # leaves another one behind.
            return None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()

    @contextmanager
    def _account_lock(self) -> Iterator[None]:
        """Serialize revision checks and mutations across threads and processes."""
        lock_path = self.directory / ".account.lock"
        self._check_no_symlink_components(lock_path)
        resolved = lock_path.resolve()
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(resolved, threading.Lock())
        with thread_lock:
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor: int | None = None
            try:
                descriptor = os.open(lock_path, flags, _MODE_FILE)
                os.fchmod(descriptor, _MODE_FILE)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ProfileStorageError(
                        f"the account lock is not a regular file: {lock_path}"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            except OSError as exc:
                raise ProfileStorageError("could not lock the account") from exc
            finally:
                if descriptor is not None:
                    with suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

    def _read_existing(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileStorageError(f"could not read the account at {path}") from exc

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        with suppress(OSError):
            descriptor = os.open(self.directory, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _with_revision(profile: AccountProfile, revision: str) -> AccountProfile:
    object.__setattr__(profile, "_revision", revision)
    return profile
