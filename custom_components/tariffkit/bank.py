"""The export credit bank, folded from a run of billing cycles.

Under Net Billing an export credit does not settle at the end of the cycle that
earned it. It banks, is spent against later cycles' charges, and carries until
the annual true-up -- which does not zero it either: ``TrueUp.closing`` is
documented as "what rolls into the next period. Never zeroed; both tariffs carry
forward." So the bank is a running quantity over the whole life of the
arrangement, and no entity computing forward from the day meters were configured
can know it.

The fold itself is :func:`tariffkit.billing.run_lifetime`, which carries the
bank between cycles and applies each annual settlement in the order it falls.
It used to live here, and did not belong: sequencing clawbacks is tariff
arithmetic, it is the kind that is wrong in ways only a second reader notices,
and the library is where such things are tested.

What is left is what a Home Assistant integration should own: checking the run
it is about to fold is contiguous, deciding which arrangement the account is
under, and refusing with a reason where the inputs cannot support an answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise

from tariffkit.account import AccountError, AccountProfile
from tariffkit.billing import Bill, CreditBalances, run_lifetime
from tariffkit.models import Supplier

_LOGGER = logging.getLogger(__name__)


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
        generation. Which buckets that means is
        :meth:`tariffkit.billing.CreditBalances.held_by`'s to say, beside the
        component-to-bucket map it has to agree with.
        """
        return float(self.balance.held_by(supplier, split=self.split))

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": round(self.balance.generation, 4),
            "delivery": round(self.balance.delivery, 4),
            "bonus": round(self.balance.bonus, 4),
            # The CCA's own bonus credit, which its statement prints as a
            # balance beside the export credit rather than folded into it.
            "cca_bonus": round(self.balance.cca_bonus, 4),
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

    :func:`tariffkit.billing.run_lifetime` does the folding, including applying
    each annual settlement to the cycles that follow it -- a true-up claws back
    credit already paid out as Net Surplus Compensation, and folding straight
    through would count the same energy twice.
    """
    if not bills:
        return BankState(CreditBalances(), None, 0, warnings=("no priced cycles",))
    chain = _contiguous(bills)
    ordered = chain.bills

    split = _is_cca(profile, ordered[-1].period.end)
    # Every epoch's PTO, not the one in force at the end: a later epoch that
    # omits the field would erase it, and a bank folded with no PTO applies no
    # annual settlement at all -- silently, with `complete: true`.
    pto = profile.pto_date
    if _suppliers_changed(profile, ordered):
        chain.warnings.append(
            "generation changed supplier inside this run, and an annual settlement "
            "settles a whole year: the balance is folded under one arrangement and "
            "cannot be trusted across the change"
        )
    lifetime = run_lifetime(ordered, pto_date=pto, is_cca=split)
    labels = [f"{event.kind} settled {event.period.end}" for event in lifetime.events]

    return BankState(
        lifetime.closing,
        (ordered[0].period.start, ordered[-1].period.end),
        len(ordered),
        true_ups=tuple(labels),
        warnings=tuple(chain.warnings),
        split=split,
    )


def _is_cca(profile: AccountProfile, on: date) -> bool:
    from tariffkit.timeutil import PACIFIC

    try:
        config = profile.config_at(datetime(on.year, on.month, on.day, 12, tzinfo=PACIFIC))
    except AccountError:
        return False
    return config.supplier is Supplier.CCA


def _suppliers_changed(profile: AccountProfile, bills: list[Bill]) -> bool:
    """Whether generation changed hands anywhere in the folded run.

    The whole run is settled under one epoch's supplier, because an annual event
    settles a *year* and the library offers no way to say that year was half one
    arrangement and half another. Where the assumption does not hold, say so
    rather than answer.
    """
    return len({_is_cca(profile, bill.period.end) for bill in bills}) > 1
