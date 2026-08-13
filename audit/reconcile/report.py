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
    out.append(
        f"  billed ${statement.electric_charges:,.2f} electric   "
        f"computed ${result.cash_due:,.2f} due"
    )
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


def render_summary(results: Sequence[Reconciliation], *, skipped: Sequence[str] = ()) -> str:
    """One line per cycle, for reading a year at a glance.

    Compares electric charges against cash due, which is what the statement
    asks for. Two adjustments, both for the same reason -- comparing unlike
    figures reports errors that are not there. The amount due includes gas and
    summary-level credits that nothing here prices, worth $54 on 2025-11; and
    the computed total is gross of export credits the cycle earned but banked
    rather than spent, worth $6 on 2026-08.
    """
    header = (
        f"{'period':<25}{'days':>5}  {'schedule':<14}"
        f"{'billed':>10}{'computed':>10}{'delta':>9}  {'kWh':>8}  verdict"
    )
    rows = [header, "-" * len(header)]

    billed_total = computed_total = kwh_total = 0.0
    for result in sorted(results, key=lambda r: r.statement.period.start):
        statement = result.statement
        billed = statement.electric_charges
        computed = result.cash_due
        kwh = statement.billed_kwh or 0.0
        billed_total += billed
        computed_total += computed
        kwh_total += kwh

        supplier = ""
        if result.config.cca is not None:
            supplier = f"/{result.config.cca.name or result.config.cca.rate_card}"
        schedule = f"{result.config.tariff}{supplier}"

        if result.ok:
            verdict = "ok"
        else:
            counts = []
            mismatches = sum(1 for c in result.failures if c.outcome is Outcome.MISMATCH)
            unmapped = len(result.failures) - mismatches
            if mismatches:
                counts.append(f"{mismatches} mismatch")
            if unmapped:
                counts.append(f"{unmapped} unmapped")
            verdict = ", ".join(counts) or "failed"

        span = f"{statement.period.start}..{statement.period.end:%m-%d}"
        rows.append(
            f"{span:<25}"
            f"{statement.period.days:>5}  {schedule:<14}"
            f"{billed:>10,.2f}{computed:>10,.2f}{computed - billed:>+9.2f}  {kwh:>8,.0f}  {verdict}"
        )

    rows.append("-" * len(header))
    rows.append(
        f"{f'{len(results)} cycles':<25}{'':>5}  {'':<14}"
        f"{billed_total:>10,.2f}{computed_total:>10,.2f}"
        f"{computed_total - billed_total:>+9.2f}  {kwh_total:>8,.0f}  "
        f"{sum(1 for r in results if r.ok)}/{len(results)} reconciled"
    )
    # Named, not merely counted. A statement that was never priced is not a
    # statement that agreed, and a summary that quietly omits it reads as
    # fuller coverage than there was.
    for note in skipped:
        rows.append(f"  not checked: {note}")
    return "\n".join(rows)
