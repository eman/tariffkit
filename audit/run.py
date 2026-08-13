"""Reconcile every statement the portal will list.

The single-statement path takes PDFs someone already downloaded. This one asks
the portal what exists and works through it, which is the difference between a
check that gets run once and a check that gets run.

Statements are written to a cache directory and, by default, deleted again. A
PG&E statement carries the service address, the account number and a remittance
scanline that embeds it; keeping a pile of them around as a side effect of a
billing check is not a trade this tool should make on the user's behalf.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import AuditError, PortalError

#: Downloaded statements land here. Under `.cache/`, which is already ignored.
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


@contextmanager
def downloaded(
    session: object,
    ref: BillRef,
    *,
    cache: Path,
    keep: bool,
) -> Iterator[Path]:
    """A statement on disk, removed afterwards unless asked to keep it."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"PGE_{ref.statement_date:%Y%m%d}.pdf"
    if not path.exists():
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
