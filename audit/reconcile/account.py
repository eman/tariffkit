"""Audit-only checks that statement evidence matches account segments."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from tariffkit.billing.engine import Segment
from tariffkit.config import Config
from tariffkit.models import Supplier
from tariffkit.providers.pge.statements import Statement

PRINTED_SCHEDULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bE-?1\b|Tiered", re.I), "E-1"),
    (re.compile(r"EV\s*2-?A", re.I), "EV2-A"),
    (re.compile(r"E-?ELEC|Electric\s+Home", re.I), "E-ELEC"),
    (re.compile(r"E-?TOU-?C", re.I), "E-TOU-C"),
    (re.compile(r"E-?TOU-?D", re.I), "E-TOU-D"),
    (re.compile(r"Time-of-Use.*4\s*-\s*9", re.I), "E-TOU-C"),
    (re.compile(r"Time-of-Use.*5\s*-\s*8", re.I), "E-TOU-D"),
)


def schedule_from_printed(printed: str) -> str | None:
    """Map PG&E's printed marketing name to a tariff code."""
    for pattern, tariff in PRINTED_SCHEDULES:
        if pattern.search(printed):
            return tariff
    return None


def _spans(segments: Sequence[Segment]) -> tuple[tuple[date, date], ...]:
    return tuple((segment.period.start, segment.period.end) for segment in segments)


def _agreement_spans(statement: Statement) -> tuple[tuple[date, date], ...]:
    return tuple(
        (agreement.period.start, agreement.period.end) for agreement in statement.agreements
    )


def _check_segment_evidence(statement: Statement, segments: Sequence[Segment]) -> list[str]:
    expected = _spans(segments)
    observed = _agreement_spans(statement)
    problems: list[str] = []
    if observed != expected:
        problems.append(
            f"statement service-agreement spans {list(observed)} do not match "
            f"profile segments {list(expected)}"
        )
        return problems
    for agreement, segment in zip(statement.agreements, segments, strict=True):
        if agreement.tariff != segment.config.tariff:
            problems.append(
                f"statement segment {agreement.start}..{agreement.end} says "
                f"{agreement.tariff!r}, profile says {segment.config.tariff!r}"
            )
    return problems


def check_against_statement(
    config: Config,
    statement: Statement,
    *,
    segments: Sequence[Segment] = (),
) -> list[str]:
    """Return every disagreement before the audit prices any interval."""
    problems = _check_segment_evidence(statement, segments) if segments else []
    configured = {segment.config.tariff for segment in segments} or {config.tariff}
    printed_names = statement.printed_schedules or (
        (statement.rate_schedule,) if statement.rate_schedule else ()
    )
    recognised = {
        tariff for tariff in (schedule_from_printed(name) for name in printed_names) if tariff
    }
    if printed_names and not recognised:
        problems.append(
            f"none of the statement's rate schedules {list(printed_names)} matches a known "
            f"tariff, so the configured {sorted(configured)} cannot be confirmed; add it to "
            "PRINTED_SCHEDULES"
        )
    elif recognised and recognised != configured:
        problems.append(
            f"configured for {sorted(configured)} but the statement was billed on "
            f"{sorted(recognised)}"
        )

    supplied_by_cca = bool(statement.cca_name)
    if supplied_by_cca and config.supplier is not Supplier.CCA:
        problems.append(
            f"configured as {config.supplier.value} but {statement.cca_name} supplied generation"
        )
    elif not supplied_by_cca and config.supplier is Supplier.CCA:
        problems.append("configured for CCA generation but the statement has no generation page")

    if config.cca is not None and statement.cca_name:
        card = (config.cca.rate_card or config.cca.name or "").lower()
        if card and card != statement.cca_name.lower():
            problems.append(
                f"configured for CCA {card!r} but the statement is from {statement.cca_name!r}"
            )
        if (
            statement.pcia_vintage is not None
            and config.cca.pcia_vintage is not None
            and statement.pcia_vintage != config.cca.pcia_vintage
        ):
            problems.append(
                f"configured PCIA vintage {config.cca.pcia_vintage} but the statement is billed "
                f"the {statement.pcia_vintage} vintage"
            )

    if (
        statement.baseline_territory
        and config.baseline_territory
        and statement.baseline_territory != config.baseline_territory
    ):
        problems.append(
            f"configured baseline territory {config.baseline_territory!r} but the statement "
            f"says {statement.baseline_territory!r}"
        )
    return problems
