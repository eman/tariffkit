"""Focused tests for public account profiles and managed persistence."""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest

from tariffkit.account import (
    AccountEpoch,
    AccountError,
    AccountObservation,
    AccountProfile,
    AccountRateEngine,
    MeterSource,
    MeterSources,
    NamedProfileRepository,
    ObservedAgreement,
    ProfileConflictError,
    ProfileNameError,
    ProfileStorageError,
    mask_account_digits,
)
from tariffkit.billing import BillingPeriod
from tariffkit.config import CcaConfig, Config
from tariffkit.errors import ConfigError
from tariffkit.export import NbtExportRates
from tariffkit.models import Supplier
from tariffkit.timeutil import PACIFIC


def profile() -> AccountProfile:
    return AccountProfile(
        (
            AccountEpoch(date(2025, 1, 1), Config(tariff="E-ELEC")),
            AccountEpoch(date(2025, 3, 1), Config(tariff="EV2-A")),
        )
    )


class TestAccountProfile:
    def test_meter_sources_round_trip_and_legacy_migration(self) -> None:
        account = AccountProfile(
            (AccountEpoch(date(2025, 1, 1), Config()),),
            meter_sources=MeterSources(
                ha=MeterSource("sensor.grid_in", "sensor.grid_out"),
                influx=MeterSource("grid_in", "grid_out"),
            ),
        )

        assert AccountProfile.from_dict(account.to_dict()) == account
        legacy = account.to_dict()
        legacy.pop("meter_sources")
        migrated = AccountProfile.from_dict(legacy)
        assert migrated.meter_sources == MeterSources()

    def test_meter_sources_are_strict_and_provider_neutral(self) -> None:
        with pytest.raises(AccountError, match="both grid_import_entity"):
            MeterSource.from_dict({"grid_import_entity": "sensor.grid_in"})
        with pytest.raises(AccountError, match="entity identifier"):
            MeterSource("sensor.grid in", "sensor.grid_out")
        with pytest.raises(AccountError, match=r"meter_sources\.ha"):
            MeterSources.from_dict({"ha": "sensor.grid_in", "influx": None})

    def test_observations_preserve_meter_sources(self) -> None:
        source = MeterSources(ha=MeterSource("sensor.grid_in", "sensor.grid_out"))
        account = AccountProfile(
            (AccountEpoch(date(2025, 1, 1), Config()),),
            meter_sources=source,
        )
        agreement = ObservedAgreement(
            provider="utility",
            statement_date=date(2025, 2, 1),
            period=BillingPeriod(date(2025, 1, 1), date(2025, 1, 31)),
            tariff="E-ELEC",
            source_digest="d" * 64,
        )
        updated = account.with_observation(AccountObservation((agreement,)))
        assert updated.meter_sources == source

    def test_home_assistant_sanitization_preserves_meter_sources(self) -> None:
        pytest.importorskip("homeassistant")
        pytest.importorskip(
            "custom_components.tariffkit.profile",
            exc_type=ModuleNotFoundError,
        )
        from custom_components.tariffkit.profile import profile_from_entry, sanitize_profile

        source = MeterSources(ha=MeterSource("sensor.grid_in", "sensor.grid_out"))
        account = AccountProfile(
            (AccountEpoch(date(2025, 1, 1), Config()),),
            name="home",
            credential_set="portal",
            meter_sources=source,
        )

        sanitized = sanitize_profile(account)
        imported = profile_from_entry({"profile": sanitized.to_dict()})
        assert sanitized.credential_set is None
        assert imported.meter_sources == source

    def test_resolves_boundaries_and_rejects_prehistory(self) -> None:
        account = profile()

        assert account.config_at(date(2025, 1, 1)).tariff == "E-ELEC"
        assert account.config_at(date(2025, 3, 1)).tariff == "EV2-A"
        with pytest.raises(AccountError, match="before the first"):
            account.config_at(date(2024, 12, 31))

    def test_requires_sorted_unique_config_snapshots(self) -> None:
        epoch = AccountEpoch(date(2025, 1, 1), Config())
        with pytest.raises(AccountError, match="sorted"):
            AccountProfile((AccountEpoch(date(2026, 1, 1), Config()), epoch))
        with pytest.raises(AccountError, match="unique"):
            AccountProfile((epoch, epoch))
        with pytest.raises(AccountError, match="Config snapshot"):
            AccountEpoch(date(2025, 1, 1), object())  # type: ignore[arg-type]

    def test_accepts_existing_cca_config_snapshot(self) -> None:
        config = Config(
            supplier=Supplier.CCA,
            cca=CcaConfig(
                name="MCE",
                rate_card="mce",
                option="light_green",
                pcia_vintage=2011,
            ),
        )
        account = AccountProfile((AccountEpoch(date(2025, 1, 1), config),))

        assert account.config_at(date(2025, 1, 1)) == config

    def test_non_nbt_history_can_omit_interconnection_facts(self) -> None:
        config = Config(
            tariff="E-ELEC",
            vintage="NBT00",
            interconnection_year=None,
            pto_date=None,
        )

        account = AccountProfile((AccountEpoch(date(2025, 1, 1), config),))

        assert account.config_at(date(2025, 1, 1)) == config

    def test_locked_nbt_requires_interconnection_year_for_acc_plus(self) -> None:
        with pytest.raises(ConfigError, match="interconnection_year"):
            NbtExportRates(
                Config(
                    tariff="E-ELEC",
                    vintage="NBT26",
                    interconnection_year=None,
                    pto_date=None,
                )
            )

    def test_segments_tile_a_mid_cycle_transition(self) -> None:
        segments = profile().segments_for(BillingPeriod(date(2025, 2, 20), date(2025, 3, 20)))

        assert [(segment.period.start, segment.period.end) for segment in segments] == [
            (date(2025, 2, 20), date(2025, 2, 28)),
            (date(2025, 3, 1), date(2025, 3, 20)),
        ]
        assert sum(segment.period.days for segment in segments) == 29

    def test_forecast_resolves_each_timestamp(self) -> None:
        account = AccountProfile(
            (
                AccountEpoch(date(2026, 1, 1), Config(tariff="E-ELEC")),
                AccountEpoch(date(2026, 3, 1), Config(tariff="EV2-A")),
            )
        )
        engine = AccountRateEngine(account)
        curve = engine.forecast(
            hours=2,
            start=datetime(2026, 2, 28, 23, tzinfo=PACIFIC),
        )

        assert engine.describe(curve[0].start)["account_effective"]["tariff"] == "E-ELEC"
        assert engine.describe(curve[1].start)["account_effective"]["tariff"] == "EV2-A"


class TestAccountEvidence:
    def test_observations_are_sanitized_and_idempotent(self) -> None:
        agreement = ObservedAgreement(
            provider="utility",
            statement_date=date(2025, 2, 1),
            period=BillingPeriod(date(2025, 1, 1), date(2025, 1, 31)),
            tariff="E-ELEC",
            supplier=Supplier.BUNDLED,
            account_suffix="****1234",
            source_digest="a" * 64,
            extraction_mode="text",
        )
        observation = AccountObservation(
            agreements=(agreement,),
            source_digest="b" * 64,
        )
        account = profile().with_observation(observation).with_observation(observation)
        encoded = json.dumps(account.to_dict())

        assert len(account.observations) == 1
        assert "123456789" not in encoded
        assert "amount_due" not in encoded
        assert account.observations[0].agreements[0].account_suffix == "****1234"
        assert mask_account_digits("1234") == "****1234"
        with pytest.raises(AccountError):
            mask_account_digits("123456789")

    def test_conflicting_observation_identity_is_rejected(self) -> None:
        first = AccountObservation(
            agreements=(
                ObservedAgreement(
                    provider="utility",
                    statement_date=date(2025, 2, 1),
                    period=BillingPeriod(date(2025, 1, 1), date(2025, 1, 31)),
                    tariff="E-ELEC",
                    source_digest="c" * 64,
                ),
            ),
            source_digest="c" * 64,
        )
        second = AccountObservation(
            agreements=(
                ObservedAgreement(
                    provider="utility",
                    statement_date=date(2025, 2, 1),
                    period=BillingPeriod(date(2025, 1, 1), date(2025, 1, 31)),
                    tariff="EV2-A",
                    source_digest="c" * 64,
                ),
            ),
            source_digest="c" * 64,
        )

        with pytest.raises(AccountError, match="same source identity"):
            profile().with_observation(first).with_observation(second)


class TestNamedProfileRepository:
    def test_round_trip_permissions_and_revision_conflicts(self, tmp_path: Path) -> None:
        repository = NamedProfileRepository(tmp_path)
        saved = repository.save("home", profile())
        path = tmp_path / "tariffkit" / "accounts" / "home.json"

        assert repository.load("home").revision == saved.revision
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "schema_version" in json.loads(path.read_text(encoding="utf-8"))

        changed = replace(saved, epochs=(AccountEpoch(date(2025, 1, 1), Config()),))
        repository.save("home", changed)
        with pytest.raises(ProfileConflictError):
            repository.save("home", saved)

    def test_does_not_change_shared_config_root_permissions(self, tmp_path: Path) -> None:
        config_home = tmp_path / "shared-config"
        config_home.mkdir(mode=0o755)
        config_home.chmod(0o755)

        NamedProfileRepository(config_home)

        assert stat.S_IMODE(config_home.stat().st_mode) == 0o755
        assert stat.S_IMODE((config_home / "tariffkit").stat().st_mode) == 0o700

    def test_concurrent_writers_cannot_both_replace_one_revision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repository = NamedProfileRepository(tmp_path)
        saved = repository.save("home", profile())
        first = replace(
            saved,
            epochs=(AccountEpoch(date(2025, 1, 1), Config(tariff="EV2-A")),),
        )
        second = replace(
            saved,
            epochs=(AccountEpoch(date(2025, 1, 1), Config(tariff="E-TOU-C")),),
        )
        barrier = Barrier(2)
        original_replace = Path.replace

        def delayed_replace(source: Path, target: Path) -> Path:
            with suppress(BrokenBarrierError):
                barrier.wait(timeout=0.2)
            return original_replace(source, target)

        def save(candidate: AccountProfile) -> str:
            try:
                repository.save("home", candidate)
            except ProfileConflictError:
                return "conflict"
            return "saved"

        monkeypatch.setattr(Path, "replace", delayed_replace)
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(save, (first, second)))

        assert sorted(outcomes) == ["conflict", "saved"]
        assert repository.load("home").config_at(date(2026, 1, 1)).tariff in {
            "EV2-A",
            "E-TOU-C",
        }

    def test_rejects_traversal_symlinks_and_corrupt_schema(self, tmp_path: Path) -> None:
        repository = NamedProfileRepository(tmp_path)
        with pytest.raises(ProfileNameError):
            repository.path_for("../escape")

        target = tmp_path / "outside.json"
        target.write_text("{}", encoding="utf-8")
        path = tmp_path / "tariffkit" / "accounts" / "home.json"
        path.symlink_to(target)
        with pytest.raises(ProfileStorageError):
            repository.load("home")

        path.unlink()
        path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        with pytest.raises(ProfileStorageError, match="validation"):
            repository.load("home")

    def test_rejects_symlinked_parent_component(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        config_home = tmp_path / "linked" / "nested"
        config_home.parent.symlink_to(outside)

        with pytest.raises(ProfileStorageError, match="symlink"):
            NamedProfileRepository(config_home)

    def test_interrupted_replacement_keeps_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repository = NamedProfileRepository(tmp_path)
        saved = repository.save("home", profile())
        path = tmp_path / "tariffkit" / "accounts" / "home.json"
        original = path.read_bytes()

        def fail_replace(self: Path, target: Path) -> Path:
            raise OSError("simulated interruption")

        changed = replace(saved, epochs=(AccountEpoch(date(2025, 1, 1), Config()),))
        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(ProfileStorageError):
            repository.save("home", changed)

        assert path.read_bytes() == original
        assert not tuple(path.parent.glob(".home.*.tmp"))
