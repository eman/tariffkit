"""Turn validated PG&E statement evidence into safe account updates.

The statement parser establishes what PG&E printed.  This module is the
provider boundary: it hashes the source PDF, keeps only masked account facts,
and never treats an unprinted account attribute as an inference.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Final

from tariffkit.account import (
    AccountEpoch,
    AccountObservation,
    AccountProfile,
    ObservedAgreement,
    mask_account_digits,
)
from tariffkit.account.errors import AccountError
from tariffkit.billing import BillingPeriod
from tariffkit.config import Config
from tariffkit.models import Supplier
from tariffkit.tariff.retail import SUPPORTED_TARIFFS as RETAIL_TARIFFS

from .statements import Statement, StatementAgreement, StatementError, read_statement

SUPPORTED_TARIFFS: Final[frozenset[str]] = frozenset(RETAIL_TARIFFS)
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_PROVIDER = "pge"

type PdfSource = bytes | bytearray | memoryview | str | Path


class ReconciliationError(AccountError):
    """Statement evidence cannot be reconciled safely."""


class RevisionMismatchError(ReconciliationError):
    """The profile is not the revision used to create a change set."""


class ChangeOutcome(StrEnum):
    """The semantic result for one observed account fact."""

    ADD = "add"
    CONFIRM = "confirm"
    CONFLICT = "conflict"
    MISSING_REQUIRED = "missing-required"


def _json_value(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Supplier):
        return value.value
    if isinstance(value, BillingPeriod):
        return {"start": value.start.isoformat(), "end": value.end.isoformat()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=str)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _profile_revision(profile: AccountProfile) -> str | None:
    return profile.revision


def _profile_fingerprint(profile: AccountProfile) -> str:
    raw = json.dumps(
        profile.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _digest(value: str, *, label: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise ReconciliationError(f"{label} must be a SHA-256 hex digest")
    return value.lower()


def hash_pdf(source: PdfSource) -> str:
    """Hash PDF bytes or a local PDF path without retaining its contents."""
    if isinstance(source, bytes):
        contents = source
    elif isinstance(source, (bytearray, memoryview)):
        contents = bytes(source)
    else:
        path = Path(source)
        try:
            contents = path.read_bytes()
        except OSError as exc:
            raise ReconciliationError(f"could not read statement PDF {path.name!r}") from exc
    return hashlib.sha256(contents).hexdigest()


def normalize_cca_identity(printed: str) -> str:
    """Normalize PG&E's marketing name to the identity used by rate cards."""
    words = re.sub(r"[^a-z0-9]+", " ", printed.casefold()).split()
    if not words:
        raise ReconciliationError("PG&E printed an empty CCA identity")
    normalized = " ".join(words)
    if normalized in {"mce", "marin clean energy"}:
        return "MCE"
    return " ".join(word.upper() for word in words)


def _masked_suffix(value: str) -> str:
    try:
        return mask_account_digits(value)
    except AccountError as exc:
        raise ReconciliationError("PG&E printed an invalid account suffix") from exc


def _validated_digest(
    statement: Statement,
    *,
    pdf: PdfSource | None,
    pdf_sha256: str | None,
) -> str:
    if pdf is not None and pdf_sha256 is not None:
        raise ReconciliationError("provide either pdf or pdf_sha256, not both")
    if pdf_sha256 is not None:
        return _digest(pdf_sha256, label="pdf_sha256")
    if pdf is not None:
        return hash_pdf(pdf)
    if statement.source:
        # Deliberately not resolved against the working directory. `source` is
        # a basename -- `parse_statement(pages, source=source.name)` -- so
        # `Path(statement.source)` picked up whatever file of that name sat in
        # the caller's cwd. PG&E names downloads predictably, so that quietly
        # bound one statement's extracted facts to another document's digest,
        # which either blocks a legitimate import as a CONFLICT or records
        # provenance for a file nobody read.
        candidate = Path(statement.source)
        if candidate.is_absolute() and candidate.is_file():
            return hash_pdf(candidate)
    raise ReconciliationError(
        "a PDF SHA-256 is required; pass pdf= or pdf_sha256=. A statement's "
        "`source` is a basename and is not resolved against the working "
        "directory, because the wrong file of that name would hash silently."
    )


def _statement_is_valid(statement: Statement) -> None:
    problems = statement.self_check()
    if problems:
        raise StatementError(
            f"{statement.source or 'statement'} failed its self-check ({len(problems)} problems)"
        )
    if not statement.agreements:
        raise StatementError("statement has no exact PG&E service-agreement spans")


def _agreement_suffix(agreement: StatementAgreement, statement: Statement) -> str | None:
    agreement_suffix = (
        _masked_suffix(agreement.account_masked) if agreement.account_masked else None
    )
    statement_suffix = (
        _masked_suffix(statement.account_masked) if statement.account_masked else None
    )
    if agreement_suffix and statement_suffix and agreement_suffix != statement_suffix:
        raise ReconciliationError("PG&E statement and agreement account suffixes disagree")
    return agreement_suffix or statement_suffix


def observe_statement(
    statement: Statement,
    *,
    pdf: PdfSource | None = None,
    pdf_sha256: str | None = None,
    observed_at: date | None = None,
) -> AccountObservation:
    """Normalize a validated statement into sanitized account evidence."""
    _statement_is_valid(statement)
    digest = _validated_digest(statement, pdf=pdf, pdf_sha256=pdf_sha256)
    is_cca = bool(statement.cca_name or statement.cca_rate_schedule)
    cca_identity = normalize_cca_identity(statement.cca_name) if statement.cca_name else None
    mode = "ocr" if statement.recognised else "text"

    normalized: list[ObservedAgreement] = []
    for agreement in statement.agreements:
        if agreement.tariff not in SUPPORTED_TARIFFS:
            raise ReconciliationError(
                f"unsupported PG&E tariff {agreement.tariff!r} in statement agreement"
            )
        normalized.append(
            ObservedAgreement(
                provider=_PROVIDER,
                statement_date=statement.statement_date,
                period=agreement.period,
                tariff=agreement.tariff,
                supplier=Supplier.CCA if is_cca else Supplier.BUNDLED,
                cca_identity=cca_identity,
                baseline_territory=agreement.baseline_territory or None,
                pcia_vintage=agreement.pcia_vintage,
                account_suffix=_agreement_suffix(agreement, statement),
                extraction_mode=mode,
                source_digest=digest,
            )
        )

    ordered = sorted(normalized, key=_agreement_key)
    if ordered[0].period.start != statement.period.start:
        raise ReconciliationError(
            "PG&E service-agreement evidence does not start at the statement's exact cycle start"
        )
    if ordered[-1].period.end != statement.period.end:
        raise ReconciliationError(
            "PG&E service-agreement evidence does not end at the statement's exact cycle end"
        )
    for previous, current in pairwise(ordered):
        if current.period.start != previous.period.end + timedelta(days=1):
            raise ReconciliationError(
                "PG&E service-agreement evidence must cover the cycle without gaps or overlaps"
            )

    return AccountObservation(
        agreements=tuple(ordered),
        source_digest=digest,
        observed_at=observed_at or statement.statement_date,
    )


def import_statement(
    path: str | Path,
    *,
    observed_at: date | None = None,
) -> AccountObservation:
    """Read and hash one local PG&E PDF, returning only sanitized evidence."""
    source = Path(path)
    return observe_statement(
        read_statement(source),
        pdf=source,
        observed_at=observed_at,
    )


@dataclass(frozen=True, slots=True)
class AccountChange:
    """One stable semantic difference between evidence and a profile."""

    outcome: ChangeOutcome
    effective: date | None
    field: str
    before: object = None
    after: object = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.field or any(char in self.field for char in "\r\n"):
            raise ReconciliationError("change field must be a single stable name")

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "effective": self.effective.isoformat() if self.effective else None,
            "field": self.field,
            "before": _json_value(self.before),
            "after": _json_value(self.after),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AccountChangeSet:
    """A revision-bound, non-persistent account update proposal."""

    profile_revision: str | None
    observation: AccountObservation
    changes: tuple[AccountChange, ...] = ()
    proposed_epochs: tuple[AccountEpoch, ...] = ()
    profile_fingerprint: str | None = None

    @property
    def can_apply(self) -> bool:
        return not any(
            change.outcome in (ChangeOutcome.CONFLICT, ChangeOutcome.MISSING_REQUIRED)
            for change in self.changes
        )

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, object]:
        ordered = sorted(
            self.changes,
            key=lambda change: (
                change.effective or date.min,
                change.field,
                change.outcome.value,
                json.dumps(_json_value(change.after), sort_keys=True),
            ),
        )
        return {
            "profile_revision": self.profile_revision,
            "changes": [change.to_dict() for change in ordered],
        }

    def json_diff(self) -> str:
        """Return the canonical JSON semantic diff."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def to_json(self) -> str:
        """Serialize the canonical JSON semantic diff."""
        return self.json_diff()

    def human_diff(self) -> str:
        """Return a stable, line-oriented semantic diff for humans."""
        ordered = sorted(
            self.changes,
            key=lambda change: (
                change.effective or date.min,
                change.field,
                change.outcome.value,
                json.dumps(_json_value(change.after), sort_keys=True),
                change.reason,
            ),
        )
        if not ordered:
            return "No account changes."
        lines: list[str] = []
        for change in ordered:
            when = change.effective.isoformat() if change.effective else "-"
            before = json.dumps(_json_value(change.before), sort_keys=True)
            after = json.dumps(_json_value(change.after), sort_keys=True)
            line = f"{change.outcome.value.upper()} {when} {change.field}: {before} -> {after}"
            if change.reason:
                line += f" ({change.reason})"
            lines.append(line)
        return "\n".join(lines)

    def apply(self, profile: AccountProfile) -> AccountProfile:
        """Apply this proposal only to the exact profile it inspected."""
        if profile.revision != self.profile_revision or (
            self.profile_fingerprint is not None
            and _profile_fingerprint(profile) != self.profile_fingerprint
        ):
            raise RevisionMismatchError(
                "profile revision differs from the revision used for this proposal"
            )
        if not self.can_apply:
            raise ReconciliationError(
                "cannot apply a change set containing conflicts or missing values"
            )
        updated = AccountProfile(
            epochs=self.proposed_epochs or profile.epochs,
            name=profile.name,
            credential_set=profile.credential_set,
            observations=(*profile.observations, self.observation),
            meter_sources=profile.meter_sources,
        )
        _validate_complete_profile(updated)
        return updated


def _validate_complete_config(config: Config) -> tuple[str, ...]:
    missing: list[str] = []
    if config.supplier is Supplier.CCA:
        if config.cca is None:
            missing.extend(("cca", "cca.option", "cca.rate_card_or_generation_rates"))
        else:
            if not config.cca.option:
                missing.append("cca.option")
            if not config.cca.complete:
                if not config.cca.generation_rates and config.cca.rate_card is None:
                    missing.append("cca.rate_card_or_generation_rates")
                if config.cca.pcia_rate is None and config.cca.pcia_vintage is None:
                    missing.append("cca.pcia_rate_or_vintage")
                if config.cca.franchise_fee_surcharge is None and config.cca.pcia_vintage is None:
                    missing.append("cca.franchise_fee_surcharge")
    return tuple(missing)


def _config_fact(config: Config, field: str) -> object:
    if field == "tariff":
        return config.tariff
    if field == "supplier":
        return config.supplier
    if field == "cca_identity":
        return (
            config.cca.name
            if config.supplier is Supplier.CCA and config.cca and config.cca.name
            else None
        )
    if field == "baseline_territory":
        return config.baseline_territory
    if field == "pcia_vintage":
        return config.cca.pcia_vintage if config.supplier is Supplier.CCA and config.cca else None
    raise ReconciliationError(f"unsupported observed field {field!r}")


def _observed_facts(agreement: ObservedAgreement) -> tuple[tuple[str, object], ...]:
    facts: list[tuple[str, object]] = []
    fields: tuple[str, ...] = ("tariff", "supplier", "cca_identity", "baseline_territory")
    if agreement.supplier is Supplier.CCA:
        fields += ("pcia_vintage",)
    for field in fields:
        value = getattr(agreement, field)
        if value is not None:
            facts.append((field, value))
    return tuple(facts)


def _agreement_key(agreement: ObservedAgreement) -> tuple[date, date]:
    return agreement.period.start, agreement.period.end


def _validate_observation_shape(
    observation: AccountObservation,
    *,
    known_suffixes: tuple[str, ...] = (),
    known_agreements: tuple[ObservedAgreement, ...] = (),
) -> tuple[tuple[ObservedAgreement, ...], tuple[AccountChange, ...]]:
    agreements = tuple(sorted(observation.agreements, key=_agreement_key))
    changes: list[AccountChange] = []
    for agreement in agreements:
        if agreement.provider != _PROVIDER:
            changes.append(
                AccountChange(
                    ChangeOutcome.CONFLICT,
                    agreement.period.start,
                    "provider",
                    before=_PROVIDER,
                    after=agreement.provider,
                    reason="evidence belongs to a different provider",
                )
            )
        if agreement.tariff not in SUPPORTED_TARIFFS:
            changes.append(
                AccountChange(
                    ChangeOutcome.CONFLICT,
                    agreement.period.start,
                    "tariff",
                    before=sorted(SUPPORTED_TARIFFS),
                    after=agreement.tariff,
                    reason="PG&E tariff is not supported by this adapter",
                )
            )
    suffixes = sorted(
        {agreement.account_suffix for agreement in agreements if agreement.account_suffix}
    )
    if len(suffixes) > 1:
        changes.append(
            AccountChange(
                ChangeOutcome.CONFLICT,
                None,
                "account_suffix",
                before=suffixes[0],
                after=suffixes[1:],
                reason="service-agreement spans print different account suffixes",
            )
        )
    if suffixes and known_suffixes and set(suffixes).isdisjoint(known_suffixes):
        changes.append(
            AccountChange(
                ChangeOutcome.CONFLICT,
                None,
                "account_suffix",
                before=known_suffixes,
                after=suffixes,
                reason="statement account suffix differs from established profile evidence",
            )
        )

    for previous, current in pairwise(agreements):
        if current.period.start > previous.period.end + timedelta(days=1):
            changes.append(
                AccountChange(
                    ChangeOutcome.MISSING_REQUIRED,
                    current.period.start,
                    "agreement_period",
                    before=previous.period.end,
                    after=current.period.start,
                    reason="service-agreement spans are not contiguous",
                )
            )
        elif current.period.start <= previous.period.end:
            previous_facts = dict(_observed_facts(previous))
            current_facts = dict(_observed_facts(current))
            conflicting = sorted(
                field
                for field in set(previous_facts) & set(current_facts)
                if previous_facts[field] != current_facts[field]
            )
            if conflicting:
                changes.append(
                    AccountChange(
                        ChangeOutcome.CONFLICT,
                        current.period.start,
                        "agreement_overlap",
                        before={field: previous_facts[field] for field in conflicting},
                        after={field: current_facts[field] for field in conflicting},
                        reason="overlapping spans print contradictory facts",
                    )
                )
    for current in agreements:
        for previous in known_agreements:
            if (
                current.period.start <= previous.period.end
                and previous.period.start <= current.period.end
            ):
                previous_facts = dict(_observed_facts(previous))
                current_facts = dict(_observed_facts(current))
                conflicting = sorted(
                    field
                    for field in set(previous_facts) & set(current_facts)
                    if previous_facts[field] != current_facts[field]
                )
                if conflicting:
                    changes.append(
                        AccountChange(
                            ChangeOutcome.CONFLICT,
                            current.period.start,
                            "agreement_overlap",
                            before={field: previous_facts[field] for field in conflicting},
                            after={field: current_facts[field] for field in conflicting},
                            reason="statement overlaps contradictory established evidence",
                        )
                    )
    return agreements, tuple(changes)


def _merge_observed(config: Config, agreement: ObservedAgreement) -> tuple[Config, tuple[str, ...]]:
    """Merge only facts printed by the agreement into an existing snapshot."""
    changes: dict[str, object] = {}
    if agreement.tariff is not None:
        changes["tariff"] = agreement.tariff
    if agreement.supplier is Supplier.BUNDLED:
        changes["supplier"] = Supplier.BUNDLED
        changes["cca"] = None
    elif agreement.supplier is Supplier.CCA:
        if config.cca is None:
            return config, ("cca", "cca.rate_card_or_generation_rates")
        cca = config.cca
        if not cca.option:
            return config, ("cca.option",)
        if not cca.generation_rates and cca.rate_card is None:
            return config, ("cca.rate_card_or_generation_rates",)
        if agreement.cca_identity is not None and not cca.name:
            cca = replace(cca, name=agreement.cca_identity)
        if agreement.pcia_vintage is not None:
            cca = replace(cca, pcia_vintage=agreement.pcia_vintage)
        changes["supplier"] = Supplier.CCA
        changes["cca"] = cca
    if agreement.baseline_territory is not None:
        changes["baseline_territory"] = agreement.baseline_territory
    return config.with_(**changes), ()


def _changes_for_facts(
    config: Config,
    agreement: ObservedAgreement,
    *,
    outcome: ChangeOutcome,
    effective: date,
) -> tuple[AccountChange, ...]:
    changes: list[AccountChange] = []
    for field, observed in _observed_facts(agreement):
        current = _config_fact(config, field)
        normalized_current = (
            normalize_cca_identity(str(current)) if field == "cca_identity" and current else current
        )
        normalized_observed = (
            normalize_cca_identity(str(observed))
            if field == "cca_identity" and observed
            else observed
        )
        if normalized_current == normalized_observed:
            actual_outcome = ChangeOutcome.CONFIRM
        elif normalized_current in (None, ""):
            # A profile can be complete without optional facts such as the
            # printed CCA identity or baseline territory.  A statement may
            # establish those facts at an existing epoch; do not turn an
            # unestablished value into a false conflict.
            actual_outcome = ChangeOutcome.ADD
        elif outcome is ChangeOutcome.CONFIRM:
            actual_outcome = ChangeOutcome.CONFLICT
        else:
            actual_outcome = ChangeOutcome.ADD
        if actual_outcome is ChangeOutcome.CONFLICT:
            changes.append(
                AccountChange(
                    actual_outcome,
                    effective,
                    field,
                    before=current,
                    after=observed,
                    reason="authoritative profile epoch disagrees with the statement",
                )
            )
        else:
            changes.append(
                AccountChange(actual_outcome, effective, field, before=current, after=observed)
            )
    return tuple(changes)


def reconcile(
    profile: AccountProfile,
    observation: AccountObservation,
) -> AccountChangeSet:
    """Compare sanitized evidence with a profile and build a safe proposal."""
    if not isinstance(profile, AccountProfile):
        raise ReconciliationError("profile must be an AccountProfile")
    if not isinstance(observation, AccountObservation):
        raise ReconciliationError("observation must be an AccountObservation")

    revision = _profile_revision(profile)
    fingerprint = _profile_fingerprint(profile)
    # Kept as its own check now that identity no longer rests on the digest.
    # Identical bytes that yield different facts mean the parser changed its
    # mind about a document, which is worth refusing rather than silently
    # recording twice. It can only fire for a statement read from a stable
    # source, such as the same saved PDF imported twice.
    source_key = observation.source_key()
    same_source = next(
        (
            evidence
            for evidence in profile.observations
            if source_key
            and evidence.source_key() == source_key
            and evidence.identity() != observation.identity()
        ),
        None,
    )
    if same_source is not None:
        return AccountChangeSet(
            revision,
            observation,
            (
                AccountChange(
                    ChangeOutcome.CONFLICT,
                    None,
                    "observation",
                    before=same_source.to_dict(),
                    after=observation.to_dict(),
                    reason="the same source document produced different extracted facts",
                ),
            ),
            profile.epochs,
            profile_fingerprint=fingerprint,
        )
    matching_evidence = next(
        (
            evidence
            for evidence in profile.observations
            if evidence.identity() == observation.identity()
        ),
        None,
    )
    if matching_evidence is not None:
        # Identity is the statement's own content, so a match means the profile
        # already holds this evidence and there is nothing to apply. The two may
        # carry different `source_digest` values -- the portal regenerates a PDF
        # on every request -- and that difference says nothing about the bill.
        return AccountChangeSet(
            revision,
            observation,
            (),
            profile.epochs,
            profile_fingerprint=fingerprint,
        )

    known_suffixes = tuple(
        sorted(
            {
                agreement.account_suffix
                for evidence in profile.observations
                for agreement in evidence.agreements
                if agreement.account_suffix
            }
        )
    )
    known_agreements = tuple(
        agreement for evidence in profile.observations for agreement in evidence.agreements
    )
    agreements, shape_changes = _validate_observation_shape(
        observation,
        known_suffixes=known_suffixes,
        known_agreements=known_agreements,
    )
    changes = list(shape_changes)
    working = list(profile.epochs)

    for agreement in agreements:
        effective = agreement.period.start
        index = next(
            (position for position, epoch in enumerate(working) if epoch.effective == effective),
            None,
        )
        active_index = max(
            (position for position, epoch in enumerate(working) if epoch.effective <= effective),
            default=-1,
        )
        if active_index < 0:
            changes.append(
                AccountChange(
                    ChangeOutcome.MISSING_REQUIRED,
                    effective,
                    "config",
                    after=None,
                    reason="no complete prior Config exists before the printed agreement start",
                )
            )
            continue

        active = working[active_index].config

        if index is not None:
            current_epoch = working[index]
            fact_changes = _changes_for_facts(
                current_epoch.config,
                agreement,
                outcome=ChangeOutcome.CONFIRM,
                effective=effective,
            )
            changes.extend(fact_changes)
            if not any(change.outcome is ChangeOutcome.CONFLICT for change in fact_changes):
                merged, merge_missing = _merge_observed(current_epoch.config, agreement)
                missing = tuple(dict.fromkeys((*merge_missing, *_validate_complete_config(merged))))
                if missing:
                    changes.extend(
                        AccountChange(
                            ChangeOutcome.MISSING_REQUIRED,
                            effective,
                            field,
                            before=None,
                            after=None,
                            reason="a missing account input must be explicitly established",
                        )
                        for field in missing
                    )
                else:
                    working[index] = AccountEpoch(effective, merged, current_epoch.note)
            continue

        merged, merge_missing = _merge_observed(active, agreement)
        missing = tuple(dict.fromkeys((*merge_missing, *_validate_complete_config(merged))))
        if missing:
            changes.extend(
                AccountChange(
                    ChangeOutcome.MISSING_REQUIRED,
                    effective,
                    field,
                    before=None,
                    after=None,
                    reason="a new CCA epoch cannot guess its generation configuration",
                )
                for field in missing
            )
            continue

        observed_differences = tuple(
            field
            for field, observed in _observed_facts(agreement)
            if _json_value(_config_fact(active, field)) != _json_value(observed)
        )
        if not observed_differences:
            changes.extend(
                _changes_for_facts(
                    active,
                    agreement,
                    outcome=ChangeOutcome.CONFIRM,
                    effective=effective,
                )
            )
            continue

        working.append(AccountEpoch(effective, merged))
        working.sort(key=lambda epoch: epoch.effective)
        changes.extend(
            _changes_for_facts(
                active,
                agreement,
                outcome=ChangeOutcome.ADD,
                effective=effective,
            )
        )

    return AccountChangeSet(
        revision,
        observation,
        _unique_changes(changes),
        tuple(working),
        profile_fingerprint=fingerprint,
    )


def _validate_complete_profile(profile: AccountProfile) -> None:
    for epoch in profile.epochs:
        missing = _validate_complete_config(epoch.config)
        if missing:
            raise ReconciliationError(
                f"updated profile has unestablished fields at {epoch.effective}: "
                f"{', '.join(missing)}"
            )


def _unique_changes(changes: list[AccountChange]) -> tuple[AccountChange, ...]:
    unique: list[AccountChange] = []
    seen: set[str] = set()
    for change in changes:
        identity = json.dumps(change.to_dict(), sort_keys=True, separators=(",", ":"))
        if identity not in seen:
            seen.add(identity)
            unique.append(change)
    return tuple(unique)


def reconcile_statement(
    profile: AccountProfile,
    statement: Statement,
    *,
    pdf: PdfSource | None = None,
    pdf_sha256: str | None = None,
    observed_at: date | None = None,
) -> AccountChangeSet:
    """Hash, normalize, and reconcile one PG&E statement in one operation."""
    return reconcile(
        profile,
        observe_statement(
            statement,
            pdf=pdf,
            pdf_sha256=pdf_sha256,
            observed_at=observed_at,
        ),
    )


__all__ = [
    "SUPPORTED_TARIFFS",
    "AccountChange",
    "AccountChangeSet",
    "ChangeOutcome",
    "PdfSource",
    "ReconciliationError",
    "RevisionMismatchError",
    "hash_pdf",
    "import_statement",
    "normalize_cca_identity",
    "observe_statement",
    "reconcile",
    "reconcile_statement",
]
