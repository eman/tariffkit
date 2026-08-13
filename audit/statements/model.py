"""What a PG&E statement says, in a shape a computed bill can be compared against.

A statement prints the same money more than once, in different arrangements, and
that redundancy is the most useful thing about it. The December 2025 / January
2026 statement says ``$333.87`` twice: once as the sum of time-of-use lines split
across the rate change on 1 January, and once as an unbundled list of tariff
components. Those two views have to agree with each other before either is worth
comparing against anything this library computed, which is what
:meth:`Statement.self_check` is for.

The four sections are not four pages. They are four *presentations*:

``SUMMARY``
    What is owed, and the split between the utility and the generation provider.
``PGE_DELIVERY``
    Time-of-use lines, one block per sub-period. A cycle spanning a rate change
    prints two blocks, which is how the statement itself confirms that
    effective-dated pricing is the right model.
``PGE_BREAKDOWN``
    The same total, unbundled into tariff components. This is the presentation
    that maps onto ``Bill.import_components``.
``CCA_GENERATION``
    The generation provider's own page. On a CCA account this is where the state
    energy surcharge prints, which is how it stayed invisible for so long while
    every line on the utility's pages reconciled.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from nem_rates.billing import BillingPeriod

#: A printed figure is rounded to the cent, so agreement is judged at half a cent
#: plus a cent per line that had to be added together to get there.
CENT = 0.005


class Section(StrEnum):
    SUMMARY = "summary"
    PGE_DELIVERY = "pge_delivery"
    PGE_BREAKDOWN = "pge_breakdown"
    CCA_GENERATION = "cca_generation"


#: Lines that restate other lines rather than adding to them. Counting these
#: would double the section's sum, and the failure is quiet: the section total
#: comes out plausibly wrong rather than obviously so.
SUBTOTAL_LABELS = frozenset({"net charges", "subtotal"})


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One printed row carrying an amount."""

    label: str
    amount: float
    section: Section
    page: int
    #: Present on a metered row: "982.126000 kWh @ $0.13500", and also on a
    #: per-day one: "29 days @ $0.79343", which is how the Base Services Charge
    #: prints. Both are quantity-times-rate, so both are kept the same way.
    quantity: float | None = None
    unit: str = ""
    rate: float | None = None
    #: Which sub-period block this row appeared under, when the section has them.
    subperiod: tuple[date, date] | None = None
    #: The sub-heading this row sits under, where one distinguishes otherwise
    #: identical labels. On the Solar Billing Plan the same three time-of-use
    #: labels print twice, once under "Energy Produced" and once under "Energy
    #: Delivered" -- export and import. Without this they look like the section
    #: boundaries overlapping, and either the credit or the charge is dropped.
    block: str = ""
    raw: str = ""

    @property
    def is_subtotal(self) -> bool:
        return self.label.strip().lower() in SUBTOTAL_LABELS

    @property
    def kwh(self) -> float | None:
        """The quantity, when it is energy rather than days."""
        return self.quantity if self.unit.lower() == "kwh" else None


@dataclass(frozen=True, slots=True)
class StatementSection:
    name: Section
    lines: tuple[StatementLine, ...] = ()
    #: The section's own printed total, when it prints one.
    printed_total: float | None = None
    total_label: str = ""

    @property
    def charged(self) -> tuple[StatementLine, ...]:
        """The rows that add up, excluding restatements of other rows."""
        return tuple(line for line in self.lines if not line.is_subtotal)

    def total(self) -> float:
        return sum(line.amount for line in self.charged)

    def find(self, label: str) -> tuple[StatementLine, ...]:
        wanted = label.strip().lower()
        return tuple(line for line in self.lines if line.label.strip().lower() == wanted)


@dataclass(frozen=True, slots=True)
class Statement:
    """One issued statement, parsed."""

    statement_date: date
    period: BillingPeriod
    amount_due: float
    #: Last four digits only. The full number is read to find the masked form and
    #: then discarded: it never reaches a fixture, a log line, or JSON output.
    account_masked: str = ""
    billed_days: int | None = None
    billed_kwh: float | None = None
    #: Gas, on a combined statement. This account burned no therms and was still
    #: billed a minimum transportation charge, so a zero-usage service is not a
    #: zero-money one. Recorded to make the amount due add up and then ignored:
    #: nothing here prices gas, so no computed component may claim it.
    #: How many service agreements the statement covers. More than one means
    #: the account changed tariff mid-cycle and the utility priced each part
    #: separately -- a count, never the identifiers themselves.
    service_agreements: int = 1
    gas_charges: float | None = None
    #: Summary-level electric adjustments, e.g. the California Climate Credit,
    #: which belong to no detail section and are not per-cycle charges.
    electric_adjustments: float | None = None
    sections: tuple[StatementSection, ...] = ()
    #: What the statement says about itself, used to catch a stale account
    #: configuration before it can produce a confident, fabricated finding.
    rate_schedule: str = ""
    cca_name: str = ""
    cca_rate_schedule: str = ""
    baseline_territory: str = ""
    pcia_vintage: int | None = None
    source: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def section(self, name: Section) -> StatementSection | None:
        for candidate in self.sections:
            if candidate.name is name:
                return candidate
        return None

    def lines(self) -> Iterator[StatementLine]:
        for section in self.sections:
            yield from section.lines

    @property
    def subperiods(self) -> tuple[tuple[date, date], ...]:
        """The rate-change blocks the utility itself split the cycle into."""
        seen: list[tuple[date, date]] = []
        for line in self.lines():
            if line.subperiod and line.subperiod not in seen:
                seen.append(line.subperiod)
        return tuple(seen)

    @property
    def electric_charges(self) -> float:
        """The part of the bill this library is actually pricing.

        The amount due is not comparable to a computed total: it carries gas,
        and it carries summary-level credits like the California Climate Credit
        that are not per-cycle charges. On 2025-11 those two make the difference
        between $213.89 and $268.13 -- a statement that reconciles line by line
        while the headline figures look $54 apart, which reads as a failure and
        is not one.
        """
        return self.amount_due - (self.gas_charges or 0.0) - (self.electric_adjustments or 0.0)

    def self_check(self) -> list[str]:
        """Problems with the parse itself, independent of any computed bill.

        Run before reconciliation and treated as fatal, because a dropped row
        produces a bill-shaped disagreement. Reporting that as a billing defect
        would destroy the only thing this harness sells, which is that its
        findings can be trusted.
        """
        problems: list[str] = []

        # Reported alone, because everything else this statement fails is a
        # consequence of it: two agreements print two delivery sections and two
        # generation sections, so the totals disagree and every label appears
        # twice. Listing ten derived complaints buries the one fact that
        # explains them, and invites fixing the symptoms.
        if self.service_agreements > 1:
            return [
                f"this statement covers {self.service_agreements} service agreements, so the "
                f"utility priced it under more than one tariff; no single configuration "
                f"describes it and it has to be checked by hand"
            ]

        for section in self.sections:
            # The summary is a running balance -- prior balance, payments
            # received, this cycle's charges -- not a list of things that add up
            # to the amount due. Summing it would fail on every statement, and a
            # check that always fails is one nobody reads.
            if section.name is Section.SUMMARY:
                continue
            if section.printed_total is None or not section.charged:
                continue
            summed = section.total()
            slack = CENT + 0.01 * len(section.charged)
            if abs(summed - section.printed_total) > slack:
                problems.append(
                    f"{section.name}: rows sum to {summed:.2f} but the section prints "
                    f"{section.printed_total:.2f} (off by {summed - section.printed_total:+.2f})"
                )

        # The utility states one number twice, in two arrangements. They must
        # agree, or one of the two presentations was mis-parsed.
        delivery = self.section(Section.PGE_DELIVERY)
        breakdown = self.section(Section.PGE_BREAKDOWN)
        if delivery and breakdown and None not in (delivery.printed_total, breakdown.printed_total):
            assert delivery.printed_total is not None and breakdown.printed_total is not None
            if abs(delivery.printed_total - breakdown.printed_total) > CENT:
                problems.append(
                    f"the two views of the utility's charges disagree: delivery detail prints "
                    f"{delivery.printed_total:.2f}, unbundled breakdown prints "
                    f"{breakdown.printed_total:.2f}"
                )

        parts = [
            s.printed_total
            for s in self.sections
            if s.name in (Section.PGE_DELIVERY, Section.CCA_GENERATION)
            and s.printed_total is not None
        ]
        # The identity is not "the electric sections add up to the bill". A PG&E
        # statement is a combined one: it can carry gas, and it carries
        # summary-level adjustments that belong to no detail section. Both are
        # named on the statement, so both are added here rather than absorbed
        # into a tolerance -- an unexplained residue should stay visible.
        expected = sum(parts) + (self.electric_adjustments or 0.0) + (self.gas_charges or 0.0)
        if parts and abs(expected - self.amount_due) > CENT:
            extra = []
            if self.electric_adjustments:
                extra.append(f"adjustments {self.electric_adjustments:+.2f}")
            if self.gas_charges:
                extra.append(f"gas {self.gas_charges:+.2f}")
            detail = f" (sections {sum(parts):.2f}, {', '.join(extra)})" if extra else ""
            problems.append(
                f"the statement's own parts sum to {expected:.2f} but it is due "
                f"{self.amount_due:.2f}{detail}; a whole section is probably missing"
            )

        if self.billed_days is not None and self.billed_days != self.period.days:
            problems.append(
                f"the statement prints {self.billed_days} billing days but its dates "
                f"{self.period.start}..{self.period.end} span {self.period.days}"
            )

        seen: set[tuple[Section, str, tuple[date, date] | None, str]] = set()
        for line in self.lines():
            key = (line.section, line.label.strip().lower(), line.subperiod, line.block)
            if key in seen:
                problems.append(
                    f"{line.section}: {line.label!r} appears twice in the same block, "
                    f"so the section boundaries probably overlap"
                )
            seen.add(key)

        return problems
