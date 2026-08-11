"""Export credit balances carried across billing cycles.

A single cycle's charges come from :mod:`nem_rates.billing.engine`, which is
pure. This is the stateful layer above it: credits earned but not spent in a
cycle bank, and offset later charges.

Credits are not fungible. PG&E's statement states the rule directly:

    1. Energy Produced credits can only offset Energy Produced charges
    2. Energy Delivered credits can only offset Energy Delivered charges
    3. Energy Export Bonus Credit can offset any and all electric charges

So a balance is three buckets, not a number, and applying it needs each charge
classified by which bucket may offset it.

**One bank at a time.** A CCA customer has two, kept separately: PG&E's and the
CCA's, each with its own balance and its own charges to offset. A :class:`Bill`
merges both providers, so applying this to one straight off the billing engine
gives an approximation -- the two banks spend in an order a merged view cannot
reproduce. Feed one provider's charges and credits to get an exact answer; the
tests do that for each half of a real statement.

Scope: this models carryover only. The annual true-up and Net Surplus
Compensation are deliberately absent -- they need a published NSC rate and the
expiry rules for unspent credits, neither of which is vendored, and neither can
be checked against a statement until a true-up cycle exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .models import Bill, BillingPeriod


class CreditBucket(StrEnum):
    """Which charges a credit may offset."""

    #: PG&E's "Energy Produced" credits, and a CCA's Energy Export Credit.
    GENERATION = "generation"
    #: PG&E's "Energy Delivered" credits.
    DELIVERY = "delivery"
    #: Offsets anything not explicitly non-bypassable.
    BONUS = "bonus"


#: Export credit component -> the bucket it banks into.
#:
#: Verified against the 2026-08-04 statement, whose credit bank splits into
#: "Energy Delivered Credits" $6.25 (the ``delivery`` component) and "Bonus
#: Credits" $1.71 (``acc_plus``), and whose CCA page reports an Energy Export
#: Credit of $9.63 (``cca_generation``).
#:
CREDIT_BUCKETS: dict[str, CreditBucket] = {
    "delivery": CreditBucket.DELIVERY,
    "generation": CreditBucket.GENERATION,
    "cca_generation": CreditBucket.GENERATION,
    "acc_plus": CreditBucket.BONUS,
}

#: Export-side components a statement spends inside the cycle instead of banking.
#:
#: MCE's 10% Solar Bonus Credit is the one vendored. The library computes it as
#: part of the export credit, but the statement prints it on the *charges* side,
#: between the cost relief credit and "Net Charges" -- so it reduces that
#: cycle's generation charges and never reaches the bank. Banking it instead
#: overstates both credits earned and credits applied, by $0.96 on the
#: 2026-08-04 statement, while coincidentally still landing on the right closing
#: balance because it was spent the same cycle.
CHARGE_OFFSETS: dict[str, CreditBucket] = {
    "cca_solar_bonus": CreditBucket.GENERATION,
}

#: Charge component -> the bucket whose credits may offset it.
#:
#: Anything absent is treated as non-offsettable. That is deliberate: a new
#: component defaults to being payable in cash rather than silently becoming
#: creditable.
CHARGE_BUCKETS: dict[str, CreditBucket] = {
    # Generation, whoever supplies it.
    "generation": CreditBucket.GENERATION,
    "cca_generation": CreditBucket.GENERATION,
    "cca_cost_relief_credit": CreditBucket.GENERATION,
    # Delivery. The statement bills these as its "Energy Delivered" block.
    "distribution": CreditBucket.DELIVERY,
    "transmission": CreditBucket.DELIVERY,
    "transmission_rate_adjustments": CreditBucket.DELIVERY,
    "reliability_services": CreditBucket.DELIVERY,
    "wildfire_hardening": CreditBucket.DELIVERY,
    "recovery_bond_charge": CreditBucket.DELIVERY,
    "recovery_bond_credit": CreditBucket.DELIVERY,
    "new_system_generation": CreditBucket.DELIVERY,
    "bundled_pcia": CreditBucket.DELIVERY,
}

#: Charges no export credit may offset, so they are payable in cash.
#:
#: The five non-bypassable charges are non-bypassable in the tariff's own sense
#: -- that is what the term means -- so not even a bonus credit reaches them.
#: PCIA, the franchise fee surcharge and the Base Services Charge are listed
#: here as the conservative reading; see ``SCOPING_VERIFIED``.
NON_OFFSETTABLE = frozenset(
    {
        "public_purpose_programs",
        "wildfire_fund_charge",
        "competition_transition_charges",
        "nuclear_decommissioning",
        "energy_cost_recovery",
        "pcia",
        "franchise_fee_surcharge",
        "baseline_credit",
    }
)

#: False while the classification above is only partly reconciled.
#:
#: What a statement has confirmed: which components bank into which bucket, and
#: that unspent credit carries forward capped by the charges available to offset.
#:
#: What it has not: where the boundary of an "Energy Delivered charge" actually
#: falls. Confirming that needs a cycle whose credits *exceed* the charges they
#: may offset, so the cap binds and the leftover is visible. On the statements
#: reconciled so far the PG&E credits were smaller than the delivery charges, so
#: the cap never bound and any classification would have produced the same bill.
SCOPING_VERIFIED = False


@dataclass(frozen=True, slots=True)
class CreditBalances:
    """A credit bank, by bucket. Never negative."""

    generation: float = 0.0
    delivery: float = 0.0
    bonus: float = 0.0

    def __post_init__(self) -> None:
        for bucket in CreditBucket:
            if self[bucket] < -1e-9:
                raise ValueError(f"{bucket} balance is negative: {self[bucket]}")

    def __getitem__(self, bucket: CreditBucket) -> float:
        return float(getattr(self, str(bucket)))

    def with_bucket(self, bucket: CreditBucket, value: float) -> CreditBalances:
        return replace(self, **{str(bucket): value})

    @property
    def total(self) -> float:
        return self.generation + self.delivery + self.bonus

    def to_dict(self) -> dict[str, float]:
        return {
            "generation": round(self.generation, 4),
            "delivery": round(self.delivery, 4),
            "bonus": round(self.bonus, 4),
            "total": round(self.total, 4),
        }


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One cycle, after credits are applied."""

    period: BillingPeriod
    opening: CreditBalances
    earned: CreditBalances
    applied: CreditBalances
    closing: CreditBalances
    #: Charges left after credits, i.e. what is actually owed.
    cash_due: float
    #: Charges before any credit was applied.
    gross_charges: float
    #: The part of ``gross_charges`` no credit could reach.
    non_offsettable: float
    #: False while the charge classification is only partly reconciled against a
    #: statement; see ``SCOPING_VERIFIED``.
    complete: bool = SCOPING_VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period.to_dict(),
            "opening": self.opening.to_dict(),
            "earned": self.earned.to_dict(),
            "applied": self.applied.to_dict(),
            "closing": self.closing.to_dict(),
            "cash_due": round(self.cash_due, 2),
            "gross_charges": round(self.gross_charges, 2),
            "non_offsettable": round(self.non_offsettable, 2),
            "complete": self.complete,
        }


def credits_earned(bill: Bill) -> CreditBalances:
    """Split a cycle's export credits into buckets.

    ``Bill.export_components`` holds credits as negative numbers so a bill sums
    directly; a bank holds them positive.
    """
    totals: dict[CreditBucket, float] = dict.fromkeys(CreditBucket, 0.0)
    for name, value in bill.export_components.items():
        bucket = CREDIT_BUCKETS.get(name)
        if bucket is not None:
            totals[bucket] += abs(value)
    return CreditBalances(
        generation=totals[CreditBucket.GENERATION],
        delivery=totals[CreditBucket.DELIVERY],
        bonus=totals[CreditBucket.BONUS],
    )


def charges_by_bucket(
    bill: Bill,
) -> tuple[dict[CreditBucket, float], float, dict[CreditBucket, float]]:
    """``({bucket: offsettable charges}, non-offsettable charges, {bucket: unspent})``.

    A component that nets out negative -- ``cca_cost_relief_credit``, or the
    recovery bond credit -- reduces its bucket rather than creating charge to
    offset elsewhere, which is how the statement prints it.

    Fixed charges are treated as non-offsettable. The Base Services Charge is a
    daily amount for grid access rather than for energy, and no reconciled
    statement has shown a credit reaching it.
    """
    offsettable: dict[CreditBucket, float] = dict.fromkeys(CreditBucket, 0.0)
    non_offsettable = sum(bill.fixed_components.values())

    for name, value in bill.import_components.items():
        bucket = CHARGE_BUCKETS.get(name)
        if bucket is None or name in NON_OFFSETTABLE:
            non_offsettable += value
        else:
            offsettable[bucket] += value

    # Spent in-cycle rather than banked; held negative on the export side.
    for name, value in bill.export_components.items():
        bucket = CHARGE_OFFSETS.get(name)
        if bucket is not None:
            offsettable[bucket] -= abs(value)

    # An in-cycle offset can wipe out its bucket's charges but no more. The
    # excess must not leak into non_offsettable: those are the non-bypassable
    # charges, the ones nothing is allowed to reduce, and a generation-scoped
    # offset reaching them would be exactly backwards. It banks instead, which
    # is the rule the statement gives for any credit it cannot spend --
    # "saved to help offset future bill charges".
    unspent: dict[CreditBucket, float] = dict.fromkeys(CreditBucket, 0.0)
    for bucket, value in offsettable.items():
        if value < 0.0:
            unspent[bucket] = -value
            offsettable[bucket] = 0.0
    return offsettable, non_offsettable, unspent


def apply_credits(bill: Bill, opening: CreditBalances | None = None) -> LedgerEntry:
    """Offset one cycle's charges with banked and newly earned credits.

    Scoped buckets are spent first, then the bonus bucket against whatever
    remains, because the bonus is the only one that can offset anything. Doing
    it the other way round would burn the flexible credit on charges a scoped
    one could have covered, and strand the scoped credit.
    """
    opening = opening or CreditBalances()
    offsettable, non_offsettable, unspent = charges_by_bucket(bill)

    # An in-cycle offset larger than the charges it was meant to cover banks the
    # remainder rather than being lost or turned into cash owed.
    earned = credits_earned(bill)
    for bucket, value in unspent.items():
        if value:
            earned = earned.with_bucket(bucket, earned[bucket] + value)

    available = CreditBalances(
        generation=opening.generation + earned.generation,
        delivery=opening.delivery + earned.delivery,
        bonus=opening.bonus + earned.bonus,
    )
    applied = CreditBalances()
    remaining = dict(offsettable)

    for bucket in (CreditBucket.GENERATION, CreditBucket.DELIVERY):
        spend = min(available[bucket], remaining[bucket])
        applied = applied.with_bucket(bucket, spend)
        remaining[bucket] -= spend

    # The bonus reaches anything still standing, except charges the tariff makes
    # non-bypassable -- that is what "non-bypassable" means.
    bonus_spend = min(available.bonus, sum(remaining.values()))
    applied = applied.with_bucket(CreditBucket.BONUS, bonus_spend)

    closing = CreditBalances(
        generation=available.generation - applied.generation,
        delivery=available.delivery - applied.delivery,
        bonus=available.bonus - applied.bonus,
    )
    gross = sum(offsettable.values()) + non_offsettable
    return LedgerEntry(
        period=bill.period,
        opening=opening,
        earned=earned,
        applied=applied,
        closing=closing,
        cash_due=gross - applied.total,
        gross_charges=gross,
        non_offsettable=non_offsettable,
    )


@dataclass(frozen=True, slots=True)
class Ledger:
    """A run of cycles with the bank carried between them."""

    entries: tuple[LedgerEntry, ...] = field(default_factory=tuple)

    @property
    def closing(self) -> CreditBalances:
        return self.entries[-1].closing if self.entries else CreditBalances()

    @property
    def cash_due(self) -> float:
        return sum(e.cash_due for e in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "closing": self.closing.to_dict(),
            "cash_due": round(self.cash_due, 2),
        }


def run_ledger(bills: Iterable[Bill], opening: CreditBalances | None = None) -> Ledger:
    """Fold ``bills`` in order, carrying the bank between cycles.

    Bills are sorted by period start, so a caller need not pre-order them. They
    are not checked for gaps or overlaps: a ledger over a discontinuous run is
    the caller's business, and a mid-year starting balance is a legitimate way
    to begin partway through a program year.
    """
    balances = opening or CreditBalances()
    entries: list[LedgerEntry] = []
    ordered: Sequence[Bill] = sorted(bills, key=lambda b: b.period.start)
    for bill in ordered:
        entry = apply_credits(bill, balances)
        entries.append(entry)
        balances = entry.closing
    return Ledger(tuple(entries))
