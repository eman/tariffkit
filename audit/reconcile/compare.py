"""Comparing a computed bill against a printed one.

:func:`reconcile` is pure -- no I/O, no network, no clock -- so the whole
comparison is testable on constructed inputs, and a reported difference can be
reproduced from the two artefacts alone.

The output separates four things that a single "mismatch" count would blur, and
the separation is the point:

``MISMATCH``
    A line both sides describe, with different numbers. The finding worth having.
``UNMAPPED_LINE``
    Something printed that no rule claims. A gap in the map, or a charge PG&E
    introduced.
``UNMAPPED_COMPONENT``
    Something computed that no rule claims. The sneakiest failure, because every
    printed line can still agree while the total is wrong, so it is checked
    explicitly rather than inferred from the total.
``NOT_COMPUTED``
    A rule exists and the bill produced none of its components. Usually a
    configuration problem, not an arithmetic one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nem_rates.billing import Bill
from nem_rates.billing.ledger import apply_credits
from nem_rates.config import Config

from ..statements.mapping import MAP, LineRule, Side, claimed_components, rule_for, split_side
from ..statements.model import Section, Statement
from .tolerance import Tolerance


class Outcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNMAPPED_LINE = "unmapped_line"
    UNMAPPED_COMPONENT = "unmapped_component"
    NOT_COMPUTED = "not_computed"


#: Components the library computes that a statement never prints as their own
#: line, with the reason. Listed rather than silently ignored: an unexplained
#: exemption is how a real omission hides.
NOT_PRINTED_SEPARATELY: Mapping[str, str] = {
    "generation": "on a CCA account the utility cancels its own generation with a "
    "Generation Credit, so neither side prints as a component",
    # Export credits *earned* are ledger inputs, not charge lines. The statement
    # prints only what was applied, and the two differ whenever credit banks --
    # 9.63 earned against 3.63 applied on 2026-08-04. The applied figures are
    # checked through `Side.APPLIED`; treating earned as an unmapped line would
    # demand a printed line that does not exist.
    "delivery": "an export credit earned; the statement prints credits applied, not earned",
    "acc_plus": "the bonus export credit earned; only the applied amount is printed",
    "cca_generation": "the CCA's export credit earned; only the applied amount is printed",
}


@dataclass(frozen=True, slots=True)
class Comparison:
    label: str
    section: Section
    outcome: Outcome
    printed: float | None = None
    computed: float | None = None
    rule: LineRule | None = None
    parts: Mapping[str, float] = field(default_factory=dict)

    @property
    def delta(self) -> float:
        if self.printed is None or self.computed is None:
            return 0.0
        return self.computed - self.printed

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.MATCH


@dataclass(frozen=True, slots=True)
class SourceDelta:
    """Two measurements of the same period, and whether the gap is expected."""

    left: str
    right: str
    imported_delta: float
    exported_delta: float
    note: str = ""
    significant: bool = False


@dataclass(frozen=True, slots=True)
class Reconciliation:
    statement: Statement
    bill: Bill
    config: Config
    comparisons: tuple[Comparison, ...] = ()
    source_deltas: tuple[SourceDelta, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[Comparison, ...]:
        return tuple(c for c in self.comparisons if not c.ok)

    @property
    def ok(self) -> bool:
        return not self.failures and not any(d.significant for d in self.source_deltas)

    @property
    def unverified_rules(self) -> tuple[str, ...]:
        seen = {c.rule.label for c in self.comparisons if c.rule and not c.rule.confirmed}
        return tuple(sorted(seen))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.statement.source,
            "period": [
                self.statement.period.start.isoformat(),
                self.statement.period.end.isoformat(),
            ],
            "tariff": self.config.tariff,
            "amount_due": self.statement.amount_due,
            "computed_total": self.bill.total,
            "ok": self.ok,
            "comparisons": [
                {
                    "label": c.label,
                    "section": c.section.value,
                    "outcome": c.outcome.value,
                    "printed": c.printed,
                    "computed": c.computed,
                    "delta": c.delta,
                    "parts": dict(c.parts),
                    "rule_verified": bool(c.rule and c.rule.confirmed),
                }
                for c in self.comparisons
            ],
            "source_deltas": [
                {
                    "left": d.left,
                    "right": d.right,
                    "imported_delta": d.imported_delta,
                    "exported_delta": d.exported_delta,
                    "note": d.note,
                    "significant": d.significant,
                }
                for d in self.source_deltas
            ],
            "notes": list(self.notes),
        }


def applied_credits(bill: Bill) -> dict[str, float]:
    """What the ledger spent this cycle, signed the way the statement prints it.

    Negative, because the statement prints these as credits against charges and
    a rule sums its components. Keeping the sign here rather than in each rule
    means no rule has to remember to subtract.
    """
    entry = apply_credits(bill)
    return {
        "generation": -entry.applied.generation,
        "delivery": -entry.applied.delivery,
        "bonus": -entry.applied.bonus,
    }


def _side(bill: Bill, side: Side) -> Mapping[str, float]:
    if side is Side.IMPORT:
        return bill.import_components
    if side is Side.EXPORT:
        return bill.export_components
    if side is Side.APPLIED:
        return applied_credits(bill)
    return bill.fixed_components


def reconcile(
    statement: Statement,
    bill: Bill,
    config: Config,
    *,
    tolerance: Tolerance | None = None,
    source_deltas: Sequence[SourceDelta] = (),
    notes: Sequence[str] = (),
) -> Reconciliation:
    """Compare a computed bill against a parsed statement, line by line."""
    allowed = tolerance or Tolerance()
    comparisons: list[Comparison] = []
    used: set[str] = set()

    for section in statement.sections:
        if section.name is Section.SUMMARY:
            continue
        # The delivery detail restates the breakdown in a different arrangement.
        # Comparing both would double-count every component, so the breakdown is
        # the one that carries component-shaped lines and the one compared.
        if section.name is Section.PGE_DELIVERY and statement.section(Section.PGE_BREAKDOWN):
            continue

        merged: dict[str, list[float]] = {}
        for line in section.charged:
            rule = rule_for(section.name, line.label)
            key = rule.label if rule else line.label
            merged.setdefault(key, []).append(line.amount)

        for label, amounts in merged.items():
            printed = sum(amounts)
            rule = rule_for(section.name, label)
            if rule is None:
                comparisons.append(
                    Comparison(label, section.name, Outcome.UNMAPPED_LINE, printed=printed)
                )
                continue

            parts: dict[str, float] = {}
            for component in rule.components:
                side, key = split_side(component, rule.side)
                value = _side(bill, side).get(key)
                if value is not None:
                    parts[key] = value
            used.update(split_side(c, rule.side)[1] for c in rule.components)
            if not parts:
                comparisons.append(
                    Comparison(
                        label, section.name, Outcome.NOT_COMPUTED, printed=printed, rule=rule
                    )
                )
                continue

            computed = sum(parts.values())
            matched = allowed.line_ok(
                printed, computed, len(rule.components), sum(abs(v) for v in parts.values())
            )
            comparisons.append(
                Comparison(
                    label,
                    section.name,
                    Outcome.MATCH if matched else Outcome.MISMATCH,
                    printed=printed,
                    computed=computed,
                    rule=rule,
                    parts=parts,
                )
            )

    # Anything computed that no rule claims. Checked explicitly because every
    # printed line can agree while the total is wrong.
    claimed = claimed_components()
    for side in Side:
        # The applied side is a view of the ledger, not a set of components the
        # bill owns, so scanning it would report the same money twice.
        if side is Side.APPLIED:
            continue
        for key, value in _side(bill, side).items():
            if (
                (side, key) in claimed
                or key in NOT_PRINTED_SEPARATELY
                or abs(value) < allowed.ignore_below
            ):
                continue
            comparisons.append(
                Comparison(
                    key,
                    Section.PGE_BREAKDOWN,
                    Outcome.UNMAPPED_COMPONENT,
                    computed=value,
                )
            )

    return Reconciliation(
        statement=statement,
        bill=bill,
        config=config,
        comparisons=tuple(comparisons),
        source_deltas=tuple(source_deltas),
        notes=tuple(notes),
    )


def unclaimed_components(bill: Bill) -> dict[str, float]:
    """Computed components no rule claims, for ``--explain`` to search over."""
    claimed = claimed_components()
    found: dict[str, float] = {}
    for side in Side:
        if side is Side.APPLIED:
            continue
        for key, value in _side(bill, side).items():
            if (side, key) not in claimed and key not in NOT_PRINTED_SEPARATELY:
                found[key] = value
    return found


__all__ = [
    "MAP",
    "Comparison",
    "Outcome",
    "Reconciliation",
    "SourceDelta",
    "reconcile",
    "unclaimed_components",
]
