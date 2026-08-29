from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from tariffkit.account import AccountEpoch, AccountObservation, AccountProfile, ObservedAgreement
from tariffkit.billing import BillingPeriod
from tariffkit.config import CcaConfig, Config
from tariffkit.models import Supplier
from tariffkit.providers.pge import (
    AccountChangeSet,
    ChangeOutcome,
    ReconciliationError,
    RevisionMismatchError,
    hash_pdf,
    normalize_cca_identity,
    observe_statement,
    reconcile,
)
from tariffkit.providers.pge.statements import parse_statement

SOURCE = "a" * 64
STATEMENT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "statements" / "synthetic_cca_ratechange.txt"
)


def agreement(
    *,
    start: date,
    end: date,
    tariff: str = "E-ELEC",
    supplier: Supplier = Supplier.BUNDLED,
    suffix: str | None = "****1234",
    cca: str | None = None,
    baseline: str | None = None,
    pcia: int | None = None,
) -> ObservedAgreement:
    return ObservedAgreement(
        provider="pge",
        statement_date=date(2026, 2, 5),
        period=BillingPeriod(start, end),
        tariff=tariff,
        supplier=supplier,
        cca_identity=cca,
        baseline_territory=baseline,
        pcia_vintage=pcia,
        account_suffix=suffix,
        extraction_mode="text",
        source_digest=SOURCE,
    )


def observation(*agreements: ObservedAgreement, source_digest: str = SOURCE) -> AccountObservation:
    return AccountObservation(agreements=agreements, source_digest=source_digest)


def test_hash_and_cca_normalization_are_stable() -> None:
    assert hash_pdf(b"statement") == hash_pdf(b"statement")
    assert normalize_cca_identity("Marin Clean Energy") == "MCE"
    assert normalize_cca_identity("East Bay Community Energy") == "EAST BAY COMMUNITY ENERGY"


def test_statement_adapter_keeps_exact_spans_and_only_sanitized_facts() -> None:
    pages = STATEMENT_FIXTURE.read_text(encoding="utf-8").split("\x0c")
    statement = parse_statement(pages)

    evidence = observe_statement(statement, pdf=b"statement")
    observed = evidence.agreements[0]

    assert observed.provider == "pge"
    assert observed.period == statement.agreements[0].period
    assert observed.supplier is Supplier.CCA
    assert observed.cca_identity == "MCE"
    assert observed.account_suffix == "****9999"
    assert observed.source_digest == hash_pdf(b"statement")
    assert "9999999999" not in evidence.to_dict().__repr__()


@pytest.mark.parametrize("second_offset", [0, 2])
def test_statement_adapter_rejects_overlapping_or_gapped_agreements(
    second_offset: int,
) -> None:
    pages = STATEMENT_FIXTURE.read_text(encoding="utf-8").split("\x0c")
    statement = parse_statement(pages)
    agreement = statement.agreements[0]
    boundary = statement.period.start + timedelta(days=10)
    first = replace(
        agreement,
        period=BillingPeriod(statement.period.start, boundary),
    )
    second = replace(
        agreement,
        period=BillingPeriod(boundary + timedelta(days=second_offset), statement.period.end),
    )

    with pytest.raises(ReconciliationError, match="without gaps or overlaps"):
        observe_statement(replace(statement, agreements=(first, second)), pdf=b"statement")


def test_reconcile_adds_only_printed_fact_and_preserves_unobserved_config() -> None:
    original = Config(
        tariff="E-ELEC",
        interconnection_year=2024,
        pto_date=date(2024, 6, 1),
        discount="none",
        baseline_code="all_electric",
    )
    profile = AccountProfile((AccountEpoch(date(2025, 1, 1), original),))
    evidence = observation(agreement(start=date(2026, 1, 1), end=date(2026, 1, 31), tariff="EV2-A"))

    proposal = reconcile(profile, evidence)

    assert isinstance(proposal, AccountChangeSet)
    assert [change.outcome for change in proposal.changes] == [
        ChangeOutcome.ADD,
        ChangeOutcome.CONFIRM,
    ]
    updated = proposal.apply(profile)
    config = updated.config_at(date(2026, 1, 1))
    assert config.tariff == "EV2-A"
    assert config.pto_date == original.pto_date
    assert config.interconnection_year == original.interconnection_year
    assert config.discount == original.discount
    assert config.baseline_code == original.baseline_code


def test_repeated_import_is_idempotent_and_diff_is_canonical() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(tariff="E-ELEC", interconnection_year=2025, pto_date=date(2025, 5, 1)),
            ),
        )
    )
    evidence = observation(
        agreement(start=date(2025, 12, 1), end=date(2025, 12, 31), tariff="E-ELEC")
    )

    first = reconcile(profile, evidence)
    updated = first.apply(profile)
    repeated = reconcile(updated, evidence)

    assert repeated.changes == ()
    assert repeated.json_diff() == repeated.json_diff()
    assert repeated.human_diff() == "No account changes."


def test_same_source_digest_with_different_facts_is_a_conflict() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(interconnection_year=2025, pto_date=date(2025, 5, 1)),
            ),
        )
    )
    original = observation(
        agreement(start=date(2025, 12, 1), end=date(2025, 12, 31), tariff="E-ELEC")
    )
    profile = reconcile(profile, original).apply(profile)
    changed = observation(
        agreement(start=date(2025, 12, 1), end=date(2025, 12, 31), tariff="EV2-A")
    )

    proposal = reconcile(profile, changed)

    assert any(
        change.outcome is ChangeOutcome.CONFLICT and change.field == "observation"
        for change in proposal.changes
    )
    with pytest.raises(ReconciliationError, match="conflicts"):
        proposal.apply(profile)


def test_suffix_mismatch_and_gap_are_typed_outcomes() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(interconnection_year=2025, pto_date=date(2025, 5, 1)),
            ),
        )
    )
    evidence = observation(
        agreement(start=date(2025, 12, 1), end=date(2025, 12, 10), suffix="****1234"),
        agreement(start=date(2025, 12, 12), end=date(2025, 12, 31), suffix="****5678"),
    )

    proposal = reconcile(profile, evidence)

    outcomes = {change.outcome for change in proposal.changes}
    assert ChangeOutcome.CONFLICT in outcomes
    assert ChangeOutcome.MISSING_REQUIRED in outcomes
    with pytest.raises(ReconciliationError, match="conflicts"):
        proposal.apply(profile)


def test_suffix_must_match_established_profile_evidence() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(interconnection_year=2025, pto_date=date(2025, 5, 1)),
            ),
        )
    )
    established = observation(
        agreement(start=date(2025, 12, 1), end=date(2025, 12, 31), suffix="****1234")
    )
    profile = reconcile(profile, established).apply(profile)
    changed_source = replace(
        established.agreements[0],
        account_suffix="****5678",
        source_digest="b" * 64,
    )

    proposal = reconcile(profile, observation(changed_source, source_digest="b" * 64))

    assert any(
        change.outcome is ChangeOutcome.CONFLICT and change.field == "account_suffix"
        for change in proposal.changes
    )


def test_non_nbt_tariff_transition_does_not_require_pto_or_interconnection() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(interconnection_year=None, vintage="NBT00", pto_date=None),
            ),
        )
    )
    evidence = observation(
        agreement(start=date(2025, 12, 1), end=date(2025, 12, 31), tariff="EV2-A")
    )

    proposal = reconcile(profile, evidence)

    missing = {change.field for change in proposal.changes}
    assert "pto_date" not in missing
    assert "interconnection_year" not in missing
    updated = proposal.apply(profile)
    assert updated.config_at(date(2025, 12, 1)).tariff == "EV2-A"


def test_revision_is_required_for_apply() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(interconnection_year=2025, pto_date=date(2025, 5, 1)),
            ),
        )
    )
    evidence = observation(agreement(start=date(2026, 1, 1), end=date(2026, 1, 31), tariff="EV2-A"))
    proposal = reconcile(profile, evidence)
    changed = replace(profile, epochs=(AccountEpoch(date(2025, 1, 1), Config(tariff="EV2-A")),))

    assert proposal.profile_revision is None
    with pytest.raises(RevisionMismatchError):
        proposal.apply(changed)


def test_cca_changes_require_existing_generation_configuration() -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(interconnection_year=2025, pto_date=date(2025, 5, 1)),
            ),
        )
    )
    evidence = observation(
        agreement(
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            supplier=Supplier.CCA,
            cca="MCE",
            pcia=2011,
        )
    )

    proposal = reconcile(profile, evidence)

    assert any(
        change.outcome is ChangeOutcome.MISSING_REQUIRED and change.field == "cca"
        for change in proposal.changes
    )


def test_cca_identity_is_confirmed_without_replacing_product_tier() -> None:
    cca = CcaConfig(
        name="MCE",
        rate_card="mce",
        option="deep_green",
        pcia_vintage=2011,
        franchise_fee_surcharge=0.01,
    )
    profile = AccountProfile(
        (
            AccountEpoch(
                date(2025, 1, 1),
                Config(
                    supplier=Supplier.CCA,
                    cca=cca,
                    interconnection_year=2025,
                    pto_date=date(2025, 5, 1),
                ),
            ),
        )
    )
    evidence = observation(
        agreement(
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            supplier=Supplier.CCA,
            cca="Marin Clean Energy",
            pcia=2011,
        )
    )

    updated = reconcile(profile, evidence).apply(profile)

    updated_cca = updated.config_at(date(2026, 1, 1)).cca
    assert updated_cca is not None
    assert updated_cca.option == "deep_green"


def test_statement_can_establish_an_optional_fact_on_an_existing_epoch() -> None:
    config = Config(
        supplier=Supplier.CCA,
        cca=CcaConfig(name="", rate_card="mce", pcia_vintage=2011),
        interconnection_year=2025,
        pto_date=date(2025, 5, 1),
    )
    profile = AccountProfile((AccountEpoch(date(2026, 1, 1), config),))
    evidence = observation(
        agreement(
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            supplier=Supplier.CCA,
            cca="MCE",
            pcia=2011,
        )
    )

    proposal = reconcile(profile, evidence)
    updated = proposal.apply(profile)

    assert any(
        change.outcome is ChangeOutcome.ADD and change.field == "cca_identity"
        for change in proposal.changes
    )
    assert updated.config_at(date(2026, 1, 1)).cca is not None
    assert updated.config_at(date(2026, 1, 1)).cca.name == "MCE"


def test_vendored_cca_snapshot_is_complete_without_copied_surcharge() -> None:
    config = Config(
        supplier=Supplier.CCA,
        cca=CcaConfig(name="MCE", rate_card="mce", pcia_vintage=2011),
        interconnection_year=2025,
        pto_date=date(2025, 5, 1),
    )
    profile = AccountProfile((AccountEpoch(date(2025, 1, 1), config),))
    evidence = observation(
        agreement(
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            tariff="EV2-A",
            supplier=Supplier.CCA,
            cca="MCE",
            pcia=2011,
        )
    )

    proposal = reconcile(profile, evidence)

    assert proposal.can_apply
    updated = proposal.apply(profile)
    assert updated.config_at(date(2026, 1, 1)).cca == config.cca


def test_a_statement_source_is_not_resolved_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Statement.source` holds a basename.

    Resolving it relatively hashed whatever file of that name sat in the
    caller's directory -- PG&E names downloads predictably -- binding one
    statement's extracted facts to another document's digest.
    """
    from tariffkit.providers.pge.reconcile import _validated_digest

    decoy = tmp_path / "bill.pdf"
    decoy.write_bytes(b"%PDF-1.4 decoy")
    monkeypatch.chdir(tmp_path)

    from tariffkit.providers.pge.statements.model import Statement

    statement = Statement(
        statement_date=date(2026, 2, 5),
        period=BillingPeriod(start=date(2025, 12, 30), end=date(2026, 1, 29)),
        amount_due=100.0,
        source="bill.pdf",
    )
    with pytest.raises(ReconciliationError, match="basename"):
        _validated_digest(statement, pdf=None, pdf_sha256=None)
