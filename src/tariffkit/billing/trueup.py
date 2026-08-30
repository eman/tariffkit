"""The annual true-up: what happens to a credit bank at the end of a year.

:mod:`tariffkit.billing.ledger` carries credits from cycle to cycle. This closes
the year on them. For a CCA account that is two separate events on two separate
calendars, which is the first thing to get right:

============ ============================ ============================
             MCE Annual Cash-Out          PG&E Relevant Period
============ ============================ ============================
ends         after the March-April cycle  on the PTO anniversary
fixed for    every customer alike         this account alone
covers       generation credits           delivery credits, ACC Plus
pays surplus yes, at MCE's NSC rate       no -- a CCA account is barred
============ ============================ ============================

So an account with a June PTO date has its MCE cash-out in April and its PG&E
true-up in June, and neither one closes the other's bank. Modelling a single
annual event would be wrong for at least one of them.

**PG&E pays a CCA account nothing.** Schedule NBT, Special Condition 5.a:

    Net Surplus Generators who receive Direct Access (DA) Service from an ESP or
    who receive Community Choice Aggregation (CCA) Service from a CCA are not
    eligible to receive NSC from PG&E but may contact their ESP or CCA Provider
    to see if they provide NSC.

Applicability is limited to "all bundled Net Surplus Generators". PG&E's
published NSC series is therefore the wrong input for this account even though
it is the only published one; see :mod:`tariffkit.data`'s ``nsc/pge.toml``.

**Credits do not expire.** This is worth stating because the opposite is widely
repeated. Schedule NBT: excess generation and delivery credits "will be carried
forward to the customer's next Relevant Period", forfeited only "on the last
true-up on NBT" if the customer leaves the tariff. MCE's Solar Billing Plan
tariff: "Any remaining export credit balance will rollover to the next relevant
period, indefinitely." The annual reset to zero belongs to NEM 2.0.

Scope, and what is still unverified: no true-up statement exists for this
account yet -- the first MCE cash-out falls after the March-April 2027 cycle and
the first PG&E Relevant Period ends on the 2027 PTO anniversary. Everything here
is read off tariff text rather than reconciled against a bill, so a
:class:`TrueUp` carries ``verified=False``. Two specific things to check when
that statement arrives are recorded on :data:`OPEN_QUESTIONS`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from ..data import read_data_text
from ..errors import ConfigError, DataError
from .ledger import CreditBalances, CreditBucket, LedgerEntry, run_ledger
from .models import Bill, BillingPeriod

#: PG&E's published Net Surplus Compensation series, used only as a stand-in.
NSC_RATE_FILE = "nsc/pge.toml"

#: The billing cycle that closes MCE's cash-out year.
#:
#: The tariff says "following the conclusion of each customer's March-April
#: billing cycle" -- a fixed calendar for every customer, unlike PG&E's
#: per-account anniversary. Identified by the months a cycle spans rather than
#: by a date, because cycle boundaries drift by a few days from year to year.
CASH_OUT_START_MONTH = 3
CASH_OUT_END_MONTH = 4

#: Cash-out at or below this is credited on the bill; above it, paid by cheque.
CHECK_THRESHOLD = 200.0

#: What a real cash-out statement needs to settle. Both are places where the
#: tariff text supports more than one reading, and guessing would be worse than
#: recording the guess.
OPEN_QUESTIONS: tuple[str, ...] = (
    "Whether the Export Credit Reversal and the NSC rate are two steps or one. "
    "Section 2.a.ii reverses the initial export credit at the average Energy "
    "Export Credit rate; section 2.a.iv.(1) then describes the NSC rate as "
    "already 'reduced by the approximate value of export credits already "
    "provided for the same surplus energy'. Applying both would deduct the same "
    "credits twice. This module applies the explicit reversal in 2.a.ii and "
    "treats the configured NSC rate as gross.",
    "Whether MCE's $5,000 annual cap and the NEM-era 'NSC rate plus $0.02/kWh' "
    "formula carry over to the Solar Billing Plan. Both appear on MCE's website "
    "under the NEM 1.0/2.0 program; neither appears in the SBP tariff text.",
)


class TrueUpKind(StrEnum):
    """Which of the two annual events this is."""

    #: MCE's Annual Cash-Out, after the March-April billing cycle.
    MCE_CASH_OUT = "mce_cash_out"
    #: PG&E's Relevant Period, ending on the PTO anniversary.
    PGE_RELEVANT_PERIOD = "pge_relevant_period"


def _nsc_series() -> tuple[dict[str, float], str]:
    raw = tomllib.loads(read_data_text(NSC_RATE_FILE))
    rates: dict[str, Any] = raw["rates"]
    return {k: float(v) for k, v in rates.items()}, str(raw["source_url"])


def published_nsc_rate(month: date) -> float:
    """PG&E's published NSC rate for a true-up month, exactly.

    This is PG&E's rate for *bundled* customers. For a CCA account it is a
    stand-in and nothing more -- see the module docstring.
    """
    rates, source = _nsc_series()
    key = f"{month.year:04d}-{month.month:02d}"
    if key not in rates:
        known = sorted(rates)
        raise DataError(
            f"no published NSC rate for {key}; the vendored series covers "
            f"{known[0]} to {known[-1]}. Re-vendor from {source} "
            f"or set nsc_rate explicitly."
        )
    return rates[key]


def nsc_rate_estimate(month: date) -> tuple[float, str]:
    """``(rate, month_key)`` -- the published rate, or the latest one before it.

    A true-up month in the future has no published rate yet, and that is the
    normal case rather than an edge one: PG&E posts a month's rate in that
    month, so the first cash-out for a 2027 period cannot be priced from a
    series vendored in 2026. Falling back to the most recent published month is
    the useful behaviour for an estimate, provided the substitution is visible
    -- the returned key says which month was actually used.

    The series has moved in a narrow band (0.02684 to 0.03396 over twenty
    months), so a stale month is a defensible stand-in. That is an observation
    about the data, not a guarantee about it.
    """
    rates, _ = _nsc_series()
    key = f"{month.year:04d}-{month.month:02d}"
    if key in rates:
        return rates[key], key
    earlier = sorted(k for k in rates if k <= key)
    if not earlier:
        known = sorted(rates)
        raise DataError(
            f"no published NSC rate at or before {key}; the vendored series "
            f"starts at {known[0]}. Set nsc_rate explicitly."
        )
    return rates[earlier[-1]], earlier[-1]


@dataclass(frozen=True, slots=True)
class TrueUp:
    """One annual close-out of a credit bank."""

    kind: TrueUpKind
    period: BillingPeriod
    opening: CreditBalances
    #: What rolls into the next period. Never zeroed; both tariffs carry forward.
    closing: CreditBalances
    imported_kwh: float
    exported_kwh: float
    #: Exported minus imported, floored at zero. Positive makes the customer a
    #: Net Surplus Generator in both tariffs' sense.
    surplus_kwh: float
    #: Whether this provider pays NSC to this account at all.
    eligible: bool
    #: Export credit clawed back so the same energy is not paid for twice.
    reversal: float = 0.0
    #: The rate used, and whether it came from config or the PG&E stand-in.
    nsc_rate: float | None = None
    nsc_payment: float = 0.0
    #: Cash actually leaving the provider: NSC net of any unabsorbed reversal.
    cash_out: float = 0.0
    #: True when the cash-out exceeds the cheque threshold.
    paid_by_check: bool = False
    #: True when ``nsc_rate`` is PG&E's published stand-in rather than a rate
    #: this provider published for this account.
    estimated: bool = False
    #: Always False: no true-up statement has been reconciled yet.
    verified: bool = False
    notes: tuple[str, ...] = ()

    @property
    def settles(self) -> bool:
        """Whether this event actually moves anything.

        An event that reverses no credit and pays no cash leaves the bank
        exactly as it found it -- PG&E's Relevant Period on a CCA account is
        this by design, under Special Condition 5.a. It still *happened*, and is
        still worth reporting, but a fold that treats it as a settlement would
        cut the other provider's year short at this date.
        """
        return bool(self.reversal) or bool(self.cash_out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "period": self.period.to_dict(),
            "opening": self.opening.to_dict(),
            "closing": self.closing.to_dict(),
            "imported_kwh": round(self.imported_kwh, 3),
            "exported_kwh": round(self.exported_kwh, 3),
            "surplus_kwh": round(self.surplus_kwh, 3),
            "eligible": self.eligible,
            "reversal": round(self.reversal, 2),
            "nsc_rate": self.nsc_rate,
            "nsc_payment": round(self.nsc_payment, 2),
            "cash_out": round(self.cash_out, 2),
            "paid_by_check": self.paid_by_check,
            "estimated": self.estimated,
            "verified": self.verified,
            "notes": list(self.notes),
        }


def _span(entries: Sequence[LedgerEntry]) -> BillingPeriod:
    return BillingPeriod(entries[0].period.start, entries[-1].period.end)


def cash_out_periods(entries: Iterable[LedgerEntry]) -> list[list[LedgerEntry]]:
    """Group cycles into MCE cash-out years.

    A year closes with the cycle that begins in March and ends in April, so the
    grouping is driven by the cycles themselves rather than by assumed dates --
    boundaries drift by a few days annually. A run that never reaches a
    March-April cycle is one open period, which is the normal case partway
    through a year.
    """
    ordered = sorted(entries, key=lambda e: e.period.start)
    groups: list[list[LedgerEntry]] = []
    current: list[LedgerEntry] = []
    for entry in ordered:
        current.append(entry)
        if (
            entry.period.start.month == CASH_OUT_START_MONTH
            and entry.period.end.month == CASH_OUT_END_MONTH
        ):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def relevant_period_end(pto_date: date, after: date) -> date:
    """The first PTO anniversary strictly after ``after``.

    PG&E's Relevant Period runs "from the customer's PTO date or anniversary
    thereof", so this is per-account rather than a fixed calendar. A 29 February
    PTO date falls back to the 28th in common years.

    The PTO date itself is the start of the first period, not the end of one, so
    it never closes a period however early ``after`` falls. Without that a ledger
    beginning in the month before interconnection would close a Relevant Period
    days after it opened.
    """
    for year in range(min(after.year, pto_date.year), max(after.year, pto_date.year) + 2):
        try:
            anniversary = pto_date.replace(year=year)
        except ValueError:  # 29 February in a common year
            anniversary = pto_date.replace(year=year, day=28)
        if anniversary > after and anniversary > pto_date:
            return anniversary
    raise ConfigError(f"could not place a PTO anniversary after {after.isoformat()}")


def average_export_rate(entries: Sequence[LedgerEntry], bucket: CreditBucket) -> float:
    """Dollars of export credit earned per kWh exported, over the period.

    This is the rate MCE reverses at: "the initial export credit will be
    reversed at the average Energy Export Credit (including Solar Bonus Credit)
    rate" (MCE Solar Billing Plan tariff, section 2.a.ii, effective 2023-12-01).

    The bracket is the whole point, and this used to claim the bonus was
    "inside ``earned`` for the generation bucket, so it is included here
    without special handling". It is not: ``cca_solar_bonus`` is a charge
    offset, spent against the cycle's generation charges rather than banked, so
    ``credits_earned`` skips it and ``earned.generation`` came back 5.00 on a
    cycle that earned 5.50. Averaging without it made the reversal too small
    and so paid out surplus the tariff says is already covered.

    PG&E's own reversal works the other way and is handled separately: Schedule
    NBT SC 5.d excludes its adder -- "the ACC Plus paid to the customer on Net
    Surplus Electricity will not be debited from the customer" -- but that is
    about a bundled account's ACC Plus, not a CCA's Solar Bonus Credit.
    """
    exported = sum(e.exported_kwh for e in entries)
    if exported <= 0.0:
        return 0.0
    earned = sum(e.earned[bucket] + e.in_cycle_offsets[bucket] for e in entries)
    return earned / exported


def mce_cash_out(
    entries: Sequence[LedgerEntry],
    nsc_rate: float | None = None,
    *,
    opening: CreditBalances | None = None,
) -> TrueUp:
    """Close one MCE cash-out year over ``entries``.

    Follows the four steps the Solar Billing Plan tariff lays out, in its order.
    Retroactive payment is not modelled as a separate step: this ledger applies
    credits against charges as each cycle is folded, so credits owed against
    earlier charges in the same year have already been applied rather than left
    outstanding to be refunded.
    """
    if not entries:
        raise ConfigError("a cash-out period needs at least one cycle")
    ordered = sorted(entries, key=lambda e: e.period.start)
    period = _span(ordered)
    opening = opening if opening is not None else ordered[0].opening
    closing = ordered[-1].closing
    imported = sum(e.imported_kwh for e in ordered)
    exported = sum(e.exported_kwh for e in ordered)
    surplus = max(exported - imported, 0.0)

    notes: list[str] = []
    if surplus <= 0.0:
        # Not a Net Surplus Generator, so no reversal and no payment. The bank
        # still rolls forward untouched.
        notes.append(
            f"imported {imported:.1f} kWh against {exported:.1f} exported, so no "
            "Net Surplus Electricity and no cash-out; the balance rolls forward."
        )
        return TrueUp(
            kind=TrueUpKind.MCE_CASH_OUT,
            period=period,
            opening=opening,
            closing=closing,
            imported_kwh=imported,
            exported_kwh=exported,
            surplus_kwh=0.0,
            eligible=False,
            notes=tuple(notes),
        )

    estimated = nsc_rate is None
    if nsc_rate is not None:
        rate = nsc_rate
    else:
        rate, used = nsc_rate_estimate(period.end)
        stale = "" if used == f"{period.end:%Y-%m}" else f" (posted for {used})"
        notes.append(
            f"NSC rate {rate}{stale} is PG&E's published rate, used as a stand-in: "
            "MCE determines its Solar Billing Plan rate at cash-out and does not "
            "publish it in advance. Set Config.nsc_rate once a statement says what "
            "was actually paid."
        )

    # Reverse the credit already given for the surplus energy, so the same
    # kilowatt-hours are not paid for twice. Charged against the balance first,
    # and against the payment for whatever the balance cannot absorb.
    reversal = surplus * average_export_rate(ordered, CreditBucket.GENERATION)
    absorbed = min(reversal, closing[CreditBucket.GENERATION])
    closing = closing.with_bucket(
        CreditBucket.GENERATION, closing[CreditBucket.GENERATION] - absorbed
    )
    gross_payment = surplus * rate
    cash_out = gross_payment - (reversal - absorbed)
    if cash_out < 0.0:
        # The tariff charges the shortfall against the NSC payment; it does not
        # say the customer then owes the difference. Floor at zero and say so.
        notes.append(
            f"the reversal exceeded the NSC payment by {abs(cash_out):.2f}; floored "
            "at zero rather than billed, which the tariff does not provide for."
        )
        cash_out = 0.0

    notes.append(OPEN_QUESTIONS[0])
    return TrueUp(
        kind=TrueUpKind.MCE_CASH_OUT,
        period=period,
        opening=opening,
        closing=closing,
        imported_kwh=imported,
        exported_kwh=exported,
        surplus_kwh=surplus,
        eligible=True,
        reversal=reversal,
        nsc_rate=rate,
        nsc_payment=gross_payment,
        cash_out=cash_out,
        paid_by_check=cash_out > CHECK_THRESHOLD,
        estimated=estimated,
        notes=tuple(notes),
    )


def pge_true_up(entries: Sequence[LedgerEntry], pto_date: date, *, is_cca: bool) -> TrueUp:
    """Close one PG&E Relevant Period over ``entries``.

    For a CCA account this settles nothing in cash: the bank carries forward and
    PG&E pays no Net Surplus Compensation, per Special Condition 5.a. For a
    bundled account the surplus test applies and PG&E's published rate is the
    real rate rather than a stand-in.
    """
    if not entries:
        raise ConfigError("a relevant period needs at least one cycle")
    ordered = sorted(entries, key=lambda e: e.period.start)
    period = _span(ordered)
    imported = sum(e.imported_kwh for e in ordered)
    exported = sum(e.exported_kwh for e in ordered)
    surplus = max(exported - imported, 0.0)
    closing = ordered[-1].closing
    anniversary = relevant_period_end(pto_date, ordered[-1].period.start)

    notes = [f"Relevant Period measured against the PTO anniversary {anniversary.isoformat()}."]
    if is_cca:
        notes.append(
            "PG&E pays no NSC on a CCA account (Schedule NBT, Special Condition "
            "5.a); the generation and delivery credits carry forward instead, "
            "and are forfeited only on leaving the NBT."
        )
        return TrueUp(
            kind=TrueUpKind.PGE_RELEVANT_PERIOD,
            period=period,
            opening=ordered[0].opening,
            closing=closing,
            imported_kwh=imported,
            exported_kwh=exported,
            surplus_kwh=surplus,
            eligible=False,
            notes=tuple(notes),
        )

    if surplus <= 0.0:
        notes.append("no Net Surplus Electricity; the balance rolls forward.")
        return TrueUp(
            kind=TrueUpKind.PGE_RELEVANT_PERIOD,
            period=period,
            opening=ordered[0].opening,
            closing=closing,
            imported_kwh=imported,
            exported_kwh=exported,
            surplus_kwh=0.0,
            eligible=False,
            notes=tuple(notes),
        )

    rate, used = nsc_rate_estimate(period.end)
    stale = used != f"{period.end:%Y-%m}"
    if stale:
        notes.append(
            f"NSC rate {rate} is PG&E's rate for {used}, the latest published; the "
            f"rate for {period.end:%Y-%m} is posted in that month."
        )
    # D.22-12-056: debit the surplus kWh at the average real-world retail export
    # compensation rate, then credit the same kWh at the NSC rate. ACC Plus paid
    # on surplus energy is explicitly not debited, so the bonus bucket is left
    # alone here.
    reversal = surplus * average_export_rate(ordered, CreditBucket.GENERATION)
    absorbed = min(reversal, closing[CreditBucket.GENERATION])
    closing = closing.with_bucket(
        CreditBucket.GENERATION, closing[CreditBucket.GENERATION] - absorbed
    )
    gross_payment = surplus * rate
    cash_out = max(gross_payment - (reversal - absorbed), 0.0)
    notes.append("ACC Plus paid on surplus energy is not debited (Schedule NBT, SC 5.d).")
    return TrueUp(
        kind=TrueUpKind.PGE_RELEVANT_PERIOD,
        period=period,
        opening=ordered[0].opening,
        closing=closing,
        imported_kwh=imported,
        exported_kwh=exported,
        surplus_kwh=surplus,
        eligible=True,
        reversal=reversal,
        nsc_rate=rate,
        nsc_payment=gross_payment,
        cash_out=cash_out,
        paid_by_check=cash_out > CHECK_THRESHOLD,
        estimated=stale,
        notes=tuple(notes),
    )


def run_true_ups(
    entries: Iterable[LedgerEntry],
    *,
    pto_date: date | None = None,
    is_cca: bool = True,
    nsc_rate: float | None = None,
) -> list[TrueUp]:
    """Every annual event a run of cycles crosses, in date order.

    Emits one Community Choice Aggregator cash-out per completed March-April
    year and one PG&E true-up per completed PTO anniversary. An incomplete
    trailing period is not emitted: a year that has not closed has not been
    trued up, and reporting it as though it had would invite reading a partial
    surplus as a settled one.

    ``is_cca`` gates both, not just the PG&E branch. A bundled customer has no
    Community Choice Aggregator to settle with, so emitting a cash-out for one
    invents a counterparty and hands the caller a clawback against a bank no CCA
    holds. It also stopped the two events double-counting: on a bundled account
    both this cash-out and ``pge_true_up`` reverse the same GENERATION credit
    for the same exported energy, because on such an account PG&E *is* the
    generation supplier and there is only one settlement to make.
    """
    ordered = sorted(entries, key=lambda e: e.period.start)
    if not ordered:
        return []

    out: list[TrueUp] = []
    if is_cca:
        for group in cash_out_periods(ordered):
            last = group[-1]
            if (
                last.period.start.month == CASH_OUT_START_MONTH
                and last.period.end.month == CASH_OUT_END_MONTH
            ):
                out.append(mce_cash_out(group, nsc_rate))

    if pto_date is not None:
        # Close the period *including* the cycle that reaches the anniversary,
        # the way the March-April cycle closes an MCE year rather than opening
        # the next one. An anniversary falls mid-cycle, and the true-up lands on
        # the statement for the cycle containing it; ending the period at the
        # cycle before would drop a month of energy and credits out of it.
        window: list[LedgerEntry] = []
        boundary = relevant_period_end(pto_date, ordered[0].period.start)
        for entry in ordered:
            window.append(entry)
            if entry.period.end >= boundary:
                out.append(pge_true_up(window, pto_date, is_cca=is_cca))
                window = []
                boundary = relevant_period_end(pto_date, entry.period.end)

    return sorted(out, key=lambda t: (t.period.end, str(t.kind)))


@dataclass(frozen=True, slots=True)
class LifetimeLedger:
    """A run of cycles folded through every annual settlement it crosses."""

    entries: tuple[LedgerEntry, ...] = ()
    events: tuple[TrueUp, ...] = ()
    closing: CreditBalances = field(default_factory=CreditBalances)

    @property
    def cash_due(self) -> float:
        return sum(entry.cash_due for entry in self.entries)

    def opening_for(self, period: BillingPeriod) -> CreditBalances:
        """The bank as one cycle opened, settlements already applied."""
        for entry in self.entries:
            if entry.period == period:
                return entry.opening
        raise KeyError(f"no cycle covering {period.start}..{period.end}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "events": [event.to_dict() for event in self.events],
            "closing": self.closing.to_dict(),
            "cash_due": round(self.cash_due, 2),
        }


def run_lifetime(
    bills: Iterable[Bill],
    *,
    pto_date: date | None = None,
    is_cca: bool = True,
    nsc_rate: float | None = None,
    opening: CreditBalances | None = None,
) -> LifetimeLedger:
    """Fold ``bills`` from end to end, applying each annual settlement in turn.

    :func:`run_ledger` carries the bank between cycles but knows nothing about
    the year closing on it; :func:`run_true_ups` finds the annual events but
    computes each one independently from whichever ledger it is handed. Neither
    alone can fold a run longer than a year, and composing them naively is
    wrong in a way that is easy to miss.

    One event at a time, deliberately. A run crossing two settlements yields a
    second event derived from a ledger that never saw the first one's clawback,
    so taking the last event's closing balance discards every earlier reversal.
    On a CCA account whose PTO anniversary falls after April -- most of the year
    -- the last event is the utility's, whose CCA branch reverses nothing at
    all, so an entire cash-out survives in the bank as credit that was already
    paid out in cash. Recomputing after each event is what makes the next one
    see the last.

    An event that settles nothing must not consume cycles either. Dropping the
    bills before it would restart the *other* provider's year from this date
    rather than its own, and a cash-out's reversal is its window's surplus, so a
    year cut from twelve months to nine claws back less than the tariff requires
    and leaves the difference in the bank.
    """
    ordered = sorted(bills, key=lambda bill: bill.period.start)
    if not ordered:
        return LifetimeLedger(closing=_balance(opening))

    balance = opening
    remaining = list(ordered)
    settled: list[LedgerEntry] = []
    events: list[TrueUp] = []
    # Keyed on the event's kind as well as its date. The two calendars are
    # unrelated -- PG&E settles on the PTO anniversary, the CCA on its own
    # cash-out year -- so with a spring PTO both can land in the same cycle. On
    # a date alone the second was filtered out as already seen, and since the
    # sort puts the cash-out first it was always the utility's event that
    # vanished from the reported settlements.
    seen: set[tuple[str, date]] = set()

    while remaining:
        entries = run_ledger(remaining, opening=balance).entries
        found = [
            event
            for event in run_true_ups(entries, pto_date=pto_date, is_cca=is_cca, nsc_rate=nsc_rate)
            if (event.kind, event.period.end) not in seen
        ]
        if not found:
            settled.extend(entries)
            closing = entries[-1].closing if entries else _balance(balance)
            return LifetimeLedger(tuple(settled), tuple(events), closing)
        first = found[0]
        seen.add((first.kind, first.period.end))
        events.append(first)
        if not first.settles:
            continue
        after = [bill for bill in remaining if bill.period.start > first.period.end]
        if len(after) == len(remaining):
            # A settling event that consumes nothing would loop forever. It
            # should not happen -- a period covers at least one cycle -- but a
            # caller refreshing on a timer is the wrong place to discover
            # otherwise.
            settled.extend(entries)
            return LifetimeLedger(tuple(settled), tuple(events), first.closing)
        settled.extend(entry for entry in entries if entry.period.end <= first.period.end)
        balance = first.closing
        remaining = after

    return LifetimeLedger(tuple(settled), tuple(events), _balance(balance))


def _balance(opening: CreditBalances | None) -> CreditBalances:
    return opening if opening is not None else CreditBalances()
