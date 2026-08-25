"""The export credit bank, folded from a run of billing cycles.

Under Net Billing an export credit does not settle at the end of the cycle that
earned it. It banks, is spent against later cycles' charges, and carries until
the annual true-up -- which does not zero it either: ``TrueUp.closing`` is
documented as "what rolls into the next period. Never zeroed; both tariffs carry
forward." So the bank is a running quantity over the whole life of the
arrangement, and no entity computing forward from the day meters were configured
can know it.

Everything here is composition. :func:`tariffkit.billing.run_ledger` applies each
cycle's credits against its charges and carries the remainder;
:func:`tariffkit.billing.run_true_ups` finds the annual events and states the
balance that rolls out of each. This module folds one against the other and
refuses where the inputs cannot support an answer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise

from tariffkit.account import AccountProfile
from tariffkit.billing import (
    Bill,
    CreditBalances,
    CreditBucket,
    LedgerEntry,
    TrueUp,
    run_ledger,
    run_true_ups,
)
from tariffkit.models import Supplier

_LOGGER = logging.getLogger(__name__)


#: Which buckets each supplier's bank holds, and on whose calendar it settles.
#:
#: On a Community Choice Aggregator account these are two banks, not one. A
#: statement prints them on separate pages -- PG&E's "Energy Delivered Credits"
#: and "Bonus Credits" against the CCA's "Energy Export Credit" -- and they
#: settle on unrelated calendars: PG&E at the Permission To Operate anniversary,
#: the CCA on its own cash-out year. Adding them gives a figure no statement
#: shows and that never settles as a whole.
#:
#: On a bundled account PG&E supplies generation too, so all three buckets are
#: its own and there is a single bank.
UTILITY_BUCKETS = (CreditBucket.DELIVERY, CreditBucket.BONUS)
GENERATION_BUCKETS = (CreditBucket.GENERATION,)


@dataclass(frozen=True, slots=True)
class BankState:
    """A credit bank and everything needed to judge it."""

    balance: CreditBalances
    #: The cycles actually folded, oldest first.
    period: tuple[date, date] | None
    cycles: int
    #: Annual events crossed, as short descriptions. Empty in a first year.
    true_ups: tuple[str, ...] = ()
    #: Why the balance is not trustworthy, or empty when it is.
    warnings: tuple[str, ...] = ()
    #: True when a Community Choice Aggregator supplies generation, which is
    #: what makes this two banks rather than one.
    split: bool = False

    @property
    def trustworthy(self) -> bool:
        return not self.warnings and self.cycles > 0

    def held_by(self, supplier: str) -> float:
        """The balance one supplier holds.

        ``utility`` is the delivering utility, ``generation`` whoever supplies
        generation. On a bundled account they are the same party, so asking for
        either returns the whole bank rather than a fraction of it.
        """
        if not self.split:
            return float(self.balance.total)
        buckets = UTILITY_BUCKETS if supplier == "utility" else GENERATION_BUCKETS
        return float(sum(self.balance[bucket] for bucket in buckets))

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": round(self.balance.generation, 4),
            "delivery": round(self.balance.delivery, 4),
            "bonus": round(self.balance.bonus, 4),
            "cycles": self.cycles,
            "from": self.period[0].isoformat() if self.period else None,
            "through": self.period[1].isoformat() if self.period else None,
            "true_ups": list(self.true_ups),
            "split_between_suppliers": self.split,
            "warnings": list(self.warnings),
            "complete": self.trustworthy,
        }


@dataclass(slots=True)
class _Chain:
    """A run of bills checked for the gaps a ledger will not check for itself."""

    bills: list[Bill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _contiguous(bills: list[Bill]) -> _Chain:
    """Reject a run with holes in it before anything folds it.

    ``run_ledger`` sorts its input and deliberately does not check: "a ledger
    over a discontinuous run is the caller's business". It is this caller's
    business, and a bank folded across a missing cycle is not merely incomplete
    -- it silently reports a balance that never existed, because the credits the
    missing cycle earned and spent are simply absent from the arithmetic.
    """
    chain = _Chain()
    ordered = sorted(bills, key=lambda bill: bill.period.start)
    for previous, current in pairwise(ordered):
        expected = previous.period.end + timedelta(days=1)
        if current.period.start > expected:
            chain.warnings.append(
                f"no priced cycle between {previous.period.end} and "
                f"{current.period.start}; a bank cannot be carried across a gap"
            )
        elif current.period.start < expected:
            chain.warnings.append(
                f"cycles {previous.period.start}..{previous.period.end} and "
                f"{current.period.start}..{current.period.end} overlap, so their "
                f"credits would be counted twice"
            )
    incomplete = [bill for bill in ordered if not bill.complete]
    if incomplete:
        chain.warnings.append(
            f"{len(incomplete)} of {len(ordered)} cycle(s) were priced from incomplete "
            f"or inexact rates, so the balance inherits that uncertainty"
        )
    chain.bills = ordered
    return chain


def fold(profile: AccountProfile, bills: list[Bill]) -> BankState:
    """The bank after ``bills``, or a refusal explaining why there is none.

    Cycles after the most recent annual event are folded onto the balance that
    event rolled out, rather than everything being folded from scratch: a
    true-up claws back credit already paid for as Net Surplus Compensation, and
    ignoring that would count the same energy twice.
    """
    if not bills:
        return BankState(CreditBalances(), None, 0, warnings=("no priced cycles",))
    chain = _contiguous(bills)
    ordered = chain.bills

    split = _is_cca(profile, ordered[-1].period.end)
    pto = _pto_of(profile, ordered[-1].period.end)
    balance, labels = _chain(ordered, pto, split=split)

    return BankState(
        balance,
        (ordered[0].period.start, ordered[-1].period.end),
        len(ordered),
        true_ups=tuple(labels),
        warnings=tuple(chain.warnings),
        split=split,
    )


def _settlements(entries: Sequence[LedgerEntry], pto: date | None, *, split: bool) -> list[TrueUp]:
    """Annual events this account actually has.

    ``run_true_ups`` emits a cash-out for every completed March-April year
    without asking who supplies generation, because it is written for the
    common case of a Community Choice Aggregator account. A bundled customer has
    no CCA to settle with, so telling them one did -- and applying its clawback
    to their bank -- is inventing a counterparty.
    """
    if pto is None:
        return []
    events = run_true_ups(entries, pto_date=pto, is_cca=split)
    if split:
        return list(events)
    return [event for event in events if str(event.kind) != "mce_cash_out"]


def _chain(bills: list[Bill], pto: date | None, *, split: bool) -> tuple[CreditBalances, list[str]]:
    """Fold ``bills``, applying every annual event in the order they fall.

    One event at a time, deliberately. ``run_true_ups`` computes each event
    independently from whichever ledger it is handed, so a run crossing two of
    them yields a second event derived from a ledger that never saw the first
    one's clawback. Taking the last event's closing balance therefore discards
    every earlier reversal -- on a CCA account whose PTO anniversary falls after
    April, which is most of the year, the last event is the utility's, whose CCA
    branch reverses nothing at all, so the whole cash-out silently survives in
    the bank as credit that was already paid out in cash.

    Recomputing after each event is what makes the next one see the last.
    """
    opening: CreditBalances | None = None
    remaining = list(bills)
    labels: list[str] = []
    while remaining:
        entries = run_ledger(remaining, opening=opening).entries
        events = _settlements(entries, pto, split=split)
        if not events:
            break
        first = events[0]
        labels.append(f"{first.kind} settled {first.period.end}")
        opening = first.closing
        after = [b for b in remaining if b.period.start > first.period.end]
        if len(after) == len(remaining):
            # An event that consumes nothing would loop forever. It should not
            # happen -- a period always covers at least one cycle -- but a
            # coordinator refresh is the wrong place to discover otherwise.
            _LOGGER.warning("annual event %s consumed no cycles; stopping the fold", first)
            break
        remaining = after
    if not remaining:
        return (opening or CreditBalances()), labels
    return run_ledger(remaining, opening=opening).entries[-1].closing, labels


def _pto_of(profile: AccountProfile, on: date) -> date | None:
    from datetime import datetime

    from tariffkit.timeutil import PACIFIC

    try:
        return profile.config_at(datetime(on.year, on.month, on.day, 12, tzinfo=PACIFIC)).pto_date
    except Exception:
        return None


def _is_cca(profile: AccountProfile, on: date) -> bool:
    from datetime import datetime

    from tariffkit.timeutil import PACIFIC

    try:
        config = profile.config_at(datetime(on.year, on.month, on.day, 12, tzinfo=PACIFIC))
    except Exception:
        return False
    return config.supplier is Supplier.CCA
