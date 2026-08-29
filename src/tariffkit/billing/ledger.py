"""Export credit balances carried across billing cycles.

A single cycle's charges come from :mod:`tariffkit.billing.engine`, which is
pure. This is the stateful layer above it: credits earned but not spent in a
cycle bank, and offset later charges.

Credits are not fungible. PG&E's statement states the rule directly:

    1. Energy Produced credits can only offset Energy Produced charges
    2. Energy Delivered credits can only offset Energy Delivered charges
    3. Energy Export Bonus Credit can offset any and all electric charges

So a balance is four buckets, not a number, and applying it needs each charge
classified by which bucket may offset it. Four rather than three because a CCA
account is credited the ACC Plus adder twice -- the utility's half is spent
where it is earned, the CCA's half banks on the CCA's own page.

**One bank at a time.** A CCA customer has two, kept separately: PG&E's and the
CCA's, each with its own balance and its own charges to offset. A :class:`Bill`
merges both providers, so applying this to one straight off the billing engine
gives an approximation -- the two banks spend in an order a merged view cannot
reproduce. Feed one provider's charges and credits to get an exact answer; this
must be done for each half of a real statement.

Scope: this models carryover within a year. Closing a year -- the annual
true-up, and Net Surplus Compensation -- lives in
:mod:`tariffkit.billing.trueup`, which is built from tariff text rather than
reconciled against a statement, because no true-up has happened on this account
yet.
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
    #: The generation supplier's own bonus credit, banked on its page and
    #: settled on its calendar. The ACC Plus adder is credited twice on a CCA
    #: account -- see ``CREDIT_BUCKETS`` -- and the two halves are not one
    #: bucket: the utility spends its half against delivery charges in the
    #: cycle that earns it, while this one accumulates untouched.
    CCA_BONUS = "cca_bonus"


#: Export credit component -> the bucket it banks into.
#:
#: Verified against the 2026-08-04 statement. Its two credit banks print:
#:
#:   PG&E "YOUR ENERGY EXPORT CREDIT BANK"
#:     Energy Delivered Credits  earned $6.25, applied -$6.25, remaining $0.00
#:     Bonus Credits             earned $1.71, applied -$1.71, remaining $0.00
#:   MCE
#:     Solar Export Credits (EEC) earned this cycle        $9.63
#:     Solar Export Bonus Credits (EEBC) earned this cycle  $1.70
#:     Current EEC Balance $9.34, Current EEBC Balance $3.29, SBP total $12.63
#:
#: The ACC Plus adder is therefore credited TWICE, once by each party at the
#: same $/kWh: the utility earns and spends its half inside the cycle, and the
#: CCA banks its own half without ever applying it -- MCE's "Energy Export
#: Bonus Credits Applied" line prints $0.00 while its EEBC balance grows by the
#: full amount every cycle ($1.59 in June plus $1.70 in July gives the printed
#: $3.29). Modelling one adder understated the CCA's bank by its whole balance
#: and no reconciled bill noticed, because a credit that is never applied never
#: reaches a charge the audit compares.
CREDIT_BUCKETS: dict[str, CreditBucket] = {
    "delivery": CreditBucket.DELIVERY,
    "generation": CreditBucket.GENERATION,
    "cca_generation": CreditBucket.GENERATION,
    "acc_plus": CreditBucket.BONUS,
    "cca_acc_plus": CreditBucket.CCA_BONUS,
    # The CCA's low-income export bonus. Banked with its other bonus credit
    # rather than its export credit: the tariff calls it a "bonus credit" and
    # pays it on top of the export credit, which is how the ACC Plus half
    # behaves too. No statement has reconciled it, so if one shows it spent on
    # a different calendar this is the line to revisit.
    "cca_care_fera_bonus": CreditBucket.CCA_BONUS,
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

#: The non-bypassable charges, exactly as Schedule NBT names them.
#:
#: Special Condition 2.f, sheet 17 (57359-E): "The following NBCs may not be
#: reduced by any credits for exports to the grid, except for the ACC Plus
#: credit: Public Purpose Program, Nuclear Decommissioning Charge, Competition
#: Transition Charge, and Wildfire Fund Charge."
#:
#: Four, not five. ``energy_cost_recovery`` was in this set and is not one of
#: them; the tariff never calls it non-bypassable.
NON_BYPASSABLE = frozenset(
    {
        "public_purpose_programs",
        "wildfire_fund_charge",
        "competition_transition_charges",
        "nuclear_decommissioning",
    }
)

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
    # The Conservation Incentive Adjustment is a distribution line -- it is the
    # rate the tariff implements the baseline credit with, and plain
    # `distribution` is already DELIVERY. Absent from this map it fell to the
    # non-offsettable default, putting a distribution charge where no credit
    # could reach it.
    "conservation_incentive_adjustment": CreditBucket.DELIVERY,
    # Reachable by the ACC Plus bonus and by nothing else. See NON_BYPASSABLE.
    **dict.fromkeys(NON_BYPASSABLE, CreditBucket.BONUS),
    # Not non-bypassable in the tariff's sense, and SC 2.d puts every other
    # charge within reach of the bonus adder.
    "energy_cost_recovery": CreditBucket.BONUS,
    "pcia": CreditBucket.BONUS,
    "franchise_fee_surcharge": CreditBucket.BONUS,
}

#: Charges no export credit may offset at all, so they are payable in cash.
#:
#: ``baseline_credit`` only. It is a credit rather than a charge, and being
#: negative it *deflates* this floor instead of raising it, which lets a
#: scoped export credit reach charges it should not. Bucketing it with the
#: distribution charges it reduces is the obvious repair and is wrong in a
#: different way: it is an import-side credit, so the bucket clamp banks the
#: excess as though the customer had exported it. Settling it needs a statement
#: whose baseline credit exceeds its distribution charges -- the same evidence
#: ``SCOPING_VERIFIED`` is waiting on -- so it stays put and stays documented.
#:
#: Every other named charge now has a bucket: the
#: non-bypassable four are reachable by the ACC Plus bonus and nothing else,
#: which is what the tariff says three separate times --
#:
#:   SC 2.c, sheet 17: "Export credits associated with the ACC Plus adder will
#:   apply to all charges (including NBC charges)."
#:   SC 2.d, sheet 17: "However, export credits associated with the ACC plus
#:   adder may be used to offset any charges incurred by the customer."
#:   SC 2, sheet 19: "The ACC Plus credit can offset all charges including the
#:   NBC charges."
#:
#: A component with no bucket still lands outside every bank, so an unrecognised
#: charge is payable in cash rather than silently creditable. That default is
#: the safety property this set used to provide.
NON_OFFSETTABLE = frozenset({"baseline_credit"})

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


#: Which buckets each party's bank holds when a CCA supplies generation.
#:
#: PG&E's "Energy Delivered Credits" and "Bonus Credits" against the CCA's
#: "Energy Export Credit" and its own bonus credit -- MCE prints the latter two
#: as the EEC and EEBC balances that sum to the SBP balance. They settle on
#: unrelated calendars -- PG&E at the Permission To Operate anniversary, the CCA
#: on its own cash-out year.
UTILITY_BUCKETS = (CreditBucket.DELIVERY, CreditBucket.BONUS)
GENERATION_BUCKETS = (CreditBucket.GENERATION, CreditBucket.CCA_BONUS)


@dataclass(frozen=True, slots=True)
class CreditBalances:
    """A credit bank, by bucket. Never negative."""

    generation: float = 0.0
    delivery: float = 0.0
    bonus: float = 0.0
    cca_bonus: float = 0.0

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
        return self.generation + self.delivery + self.bonus + self.cca_bonus

    def held_by(self, party: str, *, split: bool) -> float:
        """The balance one party holds.

        ``party`` is ``"utility"`` or ``"generation"``. Under ``split`` -- a
        Community Choice Aggregator supplying generation -- these are two banks,
        not one: a statement prints them on separate pages and they settle on
        unrelated calendars, so adding them gives a figure no statement shows
        and that never settles as a whole. Without it the delivering utility
        supplies generation too, all three buckets are its own, and either name
        returns the whole bank rather than a fraction of it.

        Lives here beside ``CREDIT_BUCKETS`` because it is the same question --
        which credits belong together -- and answering it in two places is how
        the two answers come to disagree about who owns ``GENERATION``.
        """
        if party not in ("utility", "generation"):
            raise ValueError(f"unknown party {party!r}: expected 'utility' or 'generation'")
        if not split:
            return self.total
        buckets = UTILITY_BUCKETS if party == "utility" else GENERATION_BUCKETS
        return sum(self[bucket] for bucket in buckets)

    def to_dict(self) -> dict[str, float]:
        return {
            "generation": round(self.generation, 4),
            "delivery": round(self.delivery, 4),
            "bonus": round(self.bonus, 4),
            "cca_bonus": round(self.cca_bonus, 4),
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
    #: Metered energy for the cycle. Carried here because the annual true-up
    #: tests surplus in kilowatt-hours rather than dollars -- both PG&E and MCE
    #: define a Net Surplus Generator as one whose exported energy exceeds its
    #: imported energy over the period.
    imported_kwh: float = 0.0
    exported_kwh: float = 0.0
    #: Export credits the statement spent inside this cycle instead of banking,
    #: by the bucket whose charges they reduced. MCE's Solar Bonus Credit is the
    #: one vendored. They never enter ``earned`` -- that is what makes them
    #: in-cycle -- but the annual true-up reverses at a rate that includes them,
    #: so the figure has to survive the cycle to be averaged later.
    in_cycle_offsets: CreditBalances = field(default_factory=CreditBalances)
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
            "imported_kwh": round(self.imported_kwh, 3),
            "exported_kwh": round(self.exported_kwh, 3),
            "complete": self.complete,
        }


def _spent_offsets(bill: Bill, unspent: dict[CreditBucket, float]) -> CreditBalances:
    """In-cycle offsets less the part that overran its bucket and banked."""
    gross = in_cycle_offsets(bill)
    return CreditBalances(
        generation=max(0.0, gross.generation - unspent[CreditBucket.GENERATION]),
        delivery=max(0.0, gross.delivery - unspent[CreditBucket.DELIVERY]),
        bonus=max(0.0, gross.bonus - unspent[CreditBucket.BONUS]),
        cca_bonus=max(0.0, gross.cca_bonus - unspent[CreditBucket.CCA_BONUS]),
    )


def in_cycle_offsets(bill: Bill) -> CreditBalances:
    """Export credits the statement spends this cycle rather than banking.

    The mirror of :func:`credits_earned` over ``CHARGE_OFFSETS``. These reduce
    their bucket's charges directly and never reach a balance, so nothing that
    reads ``earned`` can see them -- which is why the annual true-up, whose
    reversal rate is defined to include the Solar Bonus Credit, was averaging
    without it.
    """
    totals: dict[CreditBucket, float] = dict.fromkeys(CreditBucket, 0.0)
    for name, value in bill.export_components.items():
        bucket = CHARGE_OFFSETS.get(name)
        if bucket is not None:
            totals[bucket] += abs(value)
    return CreditBalances(
        generation=totals[CreditBucket.GENERATION],
        delivery=totals[CreditBucket.DELIVERY],
        bonus=totals[CreditBucket.BONUS],
        cca_bonus=totals[CreditBucket.CCA_BONUS],
    )


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
        cca_bonus=totals[CreditBucket.CCA_BONUS],
    )


def charges_by_bucket(
    bill: Bill,
) -> tuple[dict[CreditBucket, float], float, dict[CreditBucket, float]]:
    """``({bucket: offsettable charges}, non-offsettable charges, {bucket: unspent})``.

    A component that nets out negative -- ``cca_cost_relief_credit``, or the
    recovery bond credit -- reduces its bucket rather than creating charge to
    offset elsewhere, which is how the statement prints it.

    Fixed charges are offsettable, but only by the bonus bucket. This was the
    other way round on the evidence then available -- "no reconciled statement
    has shown a credit reaching it" -- and the 2026-07-07 statement falsified
    it: it applies $1.59 of bonus credit where the energy charges alone leave
    room for $0.92, and PG&E's own wording is that the bonus offsets anything
    not explicitly non-bypassable. The Base Services Charge is not
    non-bypassable; the charges printed as Non-Bypassable Charges are, and they
    stay out of reach.
    """
    offsettable: dict[CreditBucket, float] = dict.fromkeys(CreditBucket, 0.0)
    non_offsettable = 0.0
    offsettable[CreditBucket.BONUS] += sum(bill.fixed_components.values())

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
        cca_bonus=opening.cca_bonus + earned.cca_bonus,
    )
    applied = CreditBalances()
    remaining = dict(offsettable)

    for bucket in (CreditBucket.GENERATION, CreditBucket.DELIVERY):
        spend = min(available[bucket], remaining[bucket])
        applied = applied.with_bucket(bucket, spend)
        remaining[bucket] -= spend

    # The CCA's bonus reaches its own generation charges, and only after its
    # export credit has. That order is the statement's, not a preference: on
    # 2026-08-04 MCE applied $3.63 of Energy Export Credit and $0.00 of Energy
    # Export Bonus Credit against the same $3.63 of net generation charges.
    # Spending it first would drain the bank that the statement shows growing.
    cca_bonus_spend = min(available.cca_bonus, remaining[CreditBucket.GENERATION])
    applied = applied.with_bucket(CreditBucket.CCA_BONUS, cca_bonus_spend)
    remaining[CreditBucket.GENERATION] -= cca_bonus_spend

    # The bonus reaches anything still standing, except charges the tariff makes
    # non-bypassable -- that is what "non-bypassable" means.
    bonus_spend = min(available.bonus, sum(remaining.values()))
    applied = applied.with_bucket(CreditBucket.BONUS, bonus_spend)

    closing = CreditBalances(
        generation=available.generation - applied.generation,
        delivery=available.delivery - applied.delivery,
        bonus=available.bonus - applied.bonus,
        cca_bonus=available.cca_bonus - applied.cca_bonus,
    )
    gross = sum(offsettable.values()) + non_offsettable
    # A statement charges nothing rather than paying out. `non_offsettable` can
    # go negative on its own -- `baseline_credit` is a negative import component
    # listed there -- and at a high enough export-to-import ratio it outweighs
    # the charges beside it. Reporting a negative amount owed would contradict
    # the one thing this function exists to get right, so the shortfall stays in
    # the bank as credit that was never spent.
    unspendable = max(0.0, applied.total - gross)
    if unspendable:
        applied = _reduce(applied, unspendable)
        closing = CreditBalances(
            generation=available.generation - applied.generation,
            delivery=available.delivery - applied.delivery,
            bonus=available.bonus - applied.bonus,
            cca_bonus=available.cca_bonus - applied.cca_bonus,
        )
    return LedgerEntry(
        period=bill.period,
        opening=opening,
        earned=earned,
        applied=applied,
        closing=closing,
        cash_due=max(0.0, gross - applied.total),
        gross_charges=gross,
        non_offsettable=non_offsettable,
        imported_kwh=sum(b.imported for b in bill.buckets),
        exported_kwh=sum(b.exported for b in bill.buckets),
        # Only the part actually spent against this cycle's charges. Whatever
        # an offset could not cover has already been banked into `earned` by
        # the clamp above, so carrying the gross figure here would let the
        # annual reversal count that excess twice -- and it exceeds the charges
        # precisely in the heavy-export months that produce a surplus true-up.
        in_cycle_offsets=_spent_offsets(bill, unspent),
    )


def _reduce(applied: CreditBalances, by: float) -> CreditBalances:
    """Un-spend ``by`` dollars of credit, bonus first.

    Reverse of the order it was spent in, exactly: the bonus is the most widely
    usable bucket, so it is the one to hand back when the charges turn out not
    to have needed it, and the CCA's bonus is next because it is spent last of
    the scoped buckets. Handing back a scoped bucket ahead of one spent after it
    moves credit between the two banks -- returning delivery credit to the
    utility while leaving the CCA's bonus spent is the misattribution
    ``CCA_BONUS`` exists to prevent.
    """
    left = by
    for bucket in (
        CreditBucket.BONUS,
        CreditBucket.CCA_BONUS,
        CreditBucket.DELIVERY,
        CreditBucket.GENERATION,
    ):
        give = min(applied[bucket], left)
        applied = applied.with_bucket(bucket, applied[bucket] - give)
        left -= give
        if left <= 1e-12:
            break
    return applied


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
