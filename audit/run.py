"""Reconcile every statement the portal will list.

The single-statement path takes PDFs someone already downloaded. This one asks
the portal what exists and works through it, which is the difference between a
check that gets run once and a check that gets run.

Statements are written to a cache directory and kept there, so a later run
reads them from disk instead of downloading them again. `--discard-statements`
deletes each one after the run, as `tariffkit account sync` does with the same
flag.

The cache is owner-only, and each login keeps its statements in a directory of
its own. A file is named by its issue date, and two accounts can be billed on
the same day; sharing one directory would reconcile one account's meter against
the other's statement.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import AuditError, PortalError

#: Downloaded statements land under here, one directory per login. Under
#: `.cache/`, which is already ignored.
DEFAULT_CACHE = Path(".cache/pge/statements")

BILL_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True, slots=True)
class BillRef:
    """One row of the portal's bill list."""

    bill_id: str
    statement_date: date
    printed_amount: float

    @property
    def label(self) -> str:
        """A name for output. Never the bill id, which is session-sensitive."""
        return self.statement_date.isoformat()


def _amount(raw: str) -> float:
    return float(raw.replace("$", "").replace(",", "").strip() or 0.0)


def parse_refs(rows: Sequence[dict[str, object]]) -> list[BillRef]:
    """Turn portal rows into references, newest first.

    Rows without a parseable date or id are dropped rather than guessed at: a
    reference that cannot be dated cannot be matched to interval data, and
    inventing a date would silently price the wrong month.
    """
    refs: list[BillRef] = []
    for row in rows:
        bill_id = str(row.get("billId") or "")
        stamped = BILL_DATE.match(str(row.get("billdate") or ""))
        if not bill_id or not stamped:
            continue
        refs.append(
            BillRef(
                bill_id=bill_id,
                statement_date=date(*(int(part) for part in stamped.groups())),
                printed_amount=_amount(str(row.get("billAmount") or "")),
            )
        )
    return sorted(refs, key=lambda r: r.statement_date, reverse=True)


def select(refs: Sequence[BillRef], *, since: date | None, until: date | None) -> list[BillRef]:
    chosen = [
        ref
        for ref in refs
        if (since is None or ref.statement_date >= since)
        and (until is None or ref.statement_date <= until)
    ]
    return sorted(chosen, key=lambda r: r.statement_date)


def statement_cache(root: Path, owner: str) -> Path:
    """The directory one login's statements are kept in, readable only by you.

    ``owner`` is whatever identifies the login -- its username and account id.
    It is hashed, so the directory name does not carry it.
    """
    path = root / hashlib.sha256(owner.encode()).hexdigest()[:12]
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (root, path):
        directory.chmod(0o700)
    return path


def statement_name(ref: BillRef) -> str:
    """A cached statement's file name: its issue date and the amount listed.

    The amount is in the name because the date alone does not identify a
    statement. A corrected bill can replace the original under the same date
    between one run and the next, and a name keyed only on the date reused the
    original's copy and reconciled an amount the utility had withdrawn.
    """
    return f"PGE_{ref.statement_date:%Y%m%d}_{ref.printed_amount:.2f}.pdf"


def statement_names(refs: Sequence[BillRef]) -> list[tuple[str, bool]]:
    """A file name for each statement, and whether a cached copy can be trusted.

    Two statements listed with the same date and amount each get a name of
    their own, and both are fetched afresh: the bill id is minted per session,
    so nothing stable says which cached copy is which.
    """
    listed = Counter((ref.statement_date, ref.printed_amount) for ref in refs)
    seen: Counter[tuple[date, float]] = Counter()
    names: list[tuple[str, bool]] = []
    for ref in refs:
        key = (ref.statement_date, ref.printed_amount)
        name = statement_name(ref)
        if listed[key] == 1:
            names.append((name, True))
            continue
        seen[key] += 1
        names.append((name.replace(".pdf", f"-{seen[key]}.pdf"), False))
    return names


@contextmanager
def downloaded(
    session: object,
    ref: BillRef,
    *,
    cache: Path,
    keep: bool,
    name: str | None = None,
    reuse: bool = True,
) -> Iterator[Path]:
    """A statement on disk, owner-only, removed afterwards unless kept.

    ``reuse=False`` downloads it again even when a copy with that name exists.
    """
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cache / (name or statement_name(ref))
    if not (reuse and path.exists()):
        try:
            pdf = session.download_bill(ref.bill_id)  # type: ignore[attr-defined]
        except AuditError:
            raise
        except Exception as exc:
            # The library raises its own PortalError, which is not an
            # AuditError; unconverted it escapes the CLI's handler and exits 1,
            # reporting a portal failure as a billing discrepancy.
            raise PortalError(f"could not download this statement: {exc}") from exc
        path.write_bytes(pdf)
    # On a reused copy too: one written before the cache was private keeps
    # whatever the umask gave it.
    path.chmod(0o600)
    try:
        yield path
    finally:
        if not keep:
            path.unlink(missing_ok=True)


def fetch_refs(session: object) -> list[BillRef]:
    try:
        rows = session.bill_history()  # type: ignore[attr-defined]
    except AuditError:
        raise
    except Exception as exc:
        # Re-raised as the harness's own type so the CLI reports "could not
        # check" rather than letting an httpx error surface as exit 1, which
        # reads as a billing discrepancy.
        raise PortalError(f"the portal would not list bills: {exc}") from exc
    refs = parse_refs(rows)
    if not refs:
        raise AuditError("the portal listed no bills for this account")
    return refs
