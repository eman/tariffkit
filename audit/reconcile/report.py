"""Rendering a reconciliation for a person to read.

Ordered so the answer arrives before the evidence: the headline is whether it
reconciled, then the lines that did not, then everything else. A report whose
failures are buried among forty agreeing lines gets skimmed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .compare import Comparison, Outcome, Reconciliation

_MARK = {
    Outcome.MATCH: "ok",
    Outcome.MISMATCH: "MISMATCH",
    Outcome.UNMAPPED_LINE: "UNMAPPED LINE",
    Outcome.UNMAPPED_COMPONENT: "UNMAPPED COMPONENT",
    Outcome.NOT_COMPUTED: "NOT COMPUTED",
}


def render(result: Reconciliation, *, verbose: bool = False) -> str:
    statement = result.statement
    out: list[str] = []

    supplier = ""
    if result.config.cca is not None:
        supplier = f" / {result.config.cca.name or result.config.cca.rate_card}"
    out.append(
        f"{statement.source or 'statement'}  "
        f"{statement.period.start}..{statement.period.end} "
        f"({statement.period.days} days)  {result.config.tariff}{supplier}"
    )
    out.append(f"  billed ${statement.amount_due:,.2f}   computed ${result.bill.total:,.2f}")
    if statement.subperiods:
        spans = ", ".join(f"{a}..{b}" for a, b in statement.subperiods)
        out.append(f"  the utility split this cycle at a rate change: {spans}")

    failures = result.failures
    if failures:
        out.append("")
        for comparison in _ordered(failures):
            out.append(_line(comparison))
            for key, value in sorted(comparison.parts.items()):
                out.append(f"        {key:<38} {value:>10,.2f}")

    if verbose:
        out.append("")
        for comparison in _ordered(c for c in result.comparisons if c.ok):
            out.append(_line(comparison))

    if result.source_deltas:
        out.append("")
        out.append("  meter sources")
        for delta in result.source_deltas:
            flag = "SIGNIFICANT" if delta.significant else "expected"
            out.append(
                f"    {delta.left} vs {delta.right}: import {delta.imported_delta:+,.2f} kWh, "
                f"export {delta.exported_delta:+,.2f} kWh  [{flag}]"
                + (f"  {delta.note}" if delta.note else "")
            )

    for note in result.notes:
        out.append(f"  note: {note}")
    for warning in result.bill.warnings:
        out.append(f"  bill warning: {warning}")
    if not result.bill.complete:
        out.append("  the computed bill reports itself incomplete, so the total is not final")

    counts = dict.fromkeys(Outcome, 0)
    for comparison in result.comparisons:
        counts[comparison.outcome] += 1
    out.append("")
    out.append(
        f"  {counts[Outcome.MISMATCH]} mismatch, "
        f"{counts[Outcome.UNMAPPED_LINE]} unmapped line(s), "
        f"{counts[Outcome.UNMAPPED_COMPONENT]} unmapped component(s), "
        f"{counts[Outcome.NOT_COMPUTED]} not computed, "
        f"{counts[Outcome.MATCH]} matched"
    )

    unverified = result.unverified_rules
    if unverified:
        # A rule nobody has confirmed is a hypothesis. Saying so is the
        # difference between "this reconciles" and "this reconciles, and here is
        # how much of that is assumption".
        out.append(f"  {len(unverified)} rule(s) never confirmed: {', '.join(unverified)}")
    out.append("  RECONCILED" if result.ok else "  FAILED")
    return "\n".join(out)


def _ordered(comparisons: Iterable[Comparison]) -> list[Comparison]:
    order = list(Outcome)
    return sorted(comparisons, key=lambda c: (order.index(c.outcome), c.label))


def _line(comparison: Comparison) -> str:
    printed = "" if comparison.printed is None else f"{comparison.printed:>10,.2f}"
    computed = "" if comparison.computed is None else f"{comparison.computed:>10,.2f}"
    delta = ""
    if comparison.printed is not None and comparison.computed is not None:
        delta = f" {comparison.delta:>+8,.2f}"
    mark = _MARK[comparison.outcome]
    return f"    {comparison.label:<38} {printed} {computed}{delta}  {mark}"


def render_all(results: Sequence[Reconciliation], *, verbose: bool = False) -> str:
    blocks = [render(result, verbose=verbose) for result in results]
    reconciled = sum(1 for result in results if result.ok)
    blocks.append(f"\n{reconciled}/{len(results)} statement(s) reconciled")
    return "\n\n".join(blocks)
