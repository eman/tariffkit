"""Focused tests for named-account CLI maintenance."""

from __future__ import annotations

import argparse
import importlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from tariffkit.account import (
    AccountEpoch,
    AccountObservation,
    AccountProfile,
    MeterSource,
    MeterSources,
    NamedProfileRepository,
    ObservedAgreement,
)
from tariffkit.account.cli import migrate_existing, sync_profile
from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.cli import _mqtt_settings, _pricing_context, main
from tariffkit.config import Config
from tariffkit.errors import ConfigError
from tariffkit.models import Supplier


def observation(*, tariff: str, digest: str) -> AccountObservation:
    return AccountObservation(
        agreements=(
            ObservedAgreement(
                provider="pge",
                statement_date=date(2026, 2, 1),
                period=BillingPeriod(date(2026, 1, 1), date(2026, 1, 31)),
                tariff=tariff,
                supplier=Supplier.BUNDLED,
                source_digest=digest,
            ),
        ),
        source_digest=digest,
    )


def test_account_migration_never_probes_repository_audit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "account.toml").write_text("[[history]]\neffective = 2025-01-01\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('tariff = "EV2-A"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    profile = migrate_existing("home", config_path=config)

    assert profile.epochs[0].config.tariff == "EV2-A"


def test_account_migration_requires_explicit_existing_audit_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="legacy audit account file not found"):
        migrate_existing("home", audit_path=tmp_path / "missing.toml")


def test_account_init_update_and_export_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "account",
                "init",
                "home",
                "--effective",
                "2025-01-01",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "account",
                "update",
                "home",
                "--effective",
                "2026-01-01",
                "--tariff",
                "EV2-A",
                "--apply",
            ]
        )
        == 0
    )

    exported = tmp_path / "profile.json"
    assert main(["account", "export", "home", "--output", str(exported)]) == 0
    payload = json.loads(exported.read_text(encoding="utf-8"))
    assert [epoch["config"]["tariff"] for epoch in payload["epochs"]] == ["E-ELEC", "EV2-A"]
    assert "amount_due" not in exported.read_text(encoding="utf-8")


def test_account_update_previews_without_writing(tmp_path: Path) -> None:
    monkeypatch_config = tmp_path / "config.toml"
    monkeypatch_config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert (
            main(
                [
                    "--config",
                    str(monkeypatch_config),
                    "account",
                    "init",
                    "home",
                    "--effective",
                    "2025-01-01",
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "account",
                    "update",
                    "home",
                    "--effective",
                    "2026-01-01",
                    "--tariff",
                    "EV2-A",
                ]
            )
            == 0
        )
        assert [
            epoch.effective for epoch in NamedProfileRepository(tmp_path).load("home").epochs
        ] == [date(2025, 1, 1)]
    finally:
        monkeypatch.undo()


def test_account_source_preview_apply_and_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text(
        'tariff = "E-ELEC"\ninterconnection_year = 2026\npto_date = "2026-06-03"\n',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "account",
                "init",
                "home",
                "--effective",
                "2025-01-01",
            ]
        )
        == 0
    )
    capsys.readouterr()
    repository = NamedProfileRepository(tmp_path)
    assert repository.load("home").meter_sources == MeterSources()

    assert (
        main(
            [
                "account",
                "source",
                "home",
                "set",
                "ha",
                "--grid-import-entity",
                "sensor.grid_in",
                "--grid-export-entity",
                "sensor.grid_out",
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert repository.load("home").meter_sources == MeterSources()

    assert (
        main(
            [
                "account",
                "source",
                "home",
                "set",
                "ha",
                "--grid-import-entity",
                "sensor.grid_in",
                "--grid-export-entity",
                "sensor.grid_out",
                "--apply",
            ]
        )
        == 0
    )
    assert repository.load("home").meter_sources.ha == MeterSource(
        "sensor.grid_in", "sensor.grid_out"
    )
    capsys.readouterr()
    assert main(["account", "source", "home", "show", "ha", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["configured"] is True
    assert shown["grid_import_entity"] == "sensor.grid_in"


@pytest.mark.parametrize("source", ["ha", "influx"])
def test_bill_passes_profile_entities_and_cli_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = AccountProfile(
        (AccountEpoch(date(2025, 1, 1), Config()),),
        meter_sources=MeterSources(
            ha=MeterSource("sensor.profile_in", "sensor.profile_out"),
            influx=MeterSource("profile_in", "profile_out"),
        ),
    )
    repository = NamedProfileRepository(tmp_path)
    repository.save("home", profile)

    captured: dict[str, object] = {}
    import tariffkit.sources as sources

    class FakeSettings:
        @classmethod
        def load(cls, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

    def readings(
        _settings: object, start: object, end: object, *args: object, **kwargs: object
    ) -> list[IntervalReading]:
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        return [IntervalReading(start, imported=1.0, duration=end - start)]

    monkeypatch.setattr(
        sources,
        "HaSettings" if source == "ha" else "InfluxSettings",
        FakeSettings,
    )
    monkeypatch.setattr(
        sources,
        "read_statistics" if source == "ha" else "read_counters",
        readings,
    )

    flags = (
        ["--ha-import-entity", "sensor.cli_in", "--ha-export-entity", "sensor.cli_out"]
        if source == "ha"
        else ["--influx-import-entity", "cli_in", "--influx-export-entity", "cli_out"]
    )
    assert (
        main(
            [
                "bill",
                "--account",
                "home",
                "--source",
                source,
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-01",
                *flags,
                "--json",
            ]
        )
        == 0
    )
    assert captured["profile_source"] == (
        profile.meter_sources.ha if source == "ha" else profile.meter_sources.influx
    )
    assert captured["import_entity"] == ("sensor.cli_in" if source == "ha" else "cli_in")
    assert captured["export_entity"] == ("sensor.cli_out" if source == "ha" else "cli_out")


def test_account_import_statement_previews_then_applies(
    tmp_path: Path, monkeypatch: object
) -> None:
    repository = NamedProfileRepository(tmp_path)
    repository.save(
        "home",
        AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)),
    )
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF synthetic")
    imported = observation(tariff="EV2-A", digest="a" * 64)

    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")

    monkeypatch.setattr(reconcile_module, "import_statement", lambda _path: imported)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert main(["account", "import-statement", "home", str(pdf), "--json"]) == 0
    assert len(repository.load("home").epochs) == 1
    assert main(["account", "import-statement", "home", str(pdf), "--apply"]) == 0
    assert [epoch.config.tariff for epoch in repository.load("home").epochs] == [
        "E-ELEC",
        "EV2-A",
    ]


def test_account_sync_removes_private_cache_after_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = NamedProfileRepository(tmp_path)
    repository.save(
        "home",
        AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)),
    )
    imported = observation(tariff="EV2-A", digest="b" * 64)
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def bill_history(self) -> list[dict[str, str]]:
            return [{"billId": "bill-1", "billDate": "2026-02-01"}]

        def download_bill(self, _bill_id: str) -> bytes:
            return b"%PDF synthetic"

    import tariffkit.sources.pge as pge_module

    monkeypatch.setattr(pge_module, "PgeSession", lambda _settings: Session())
    monkeypatch.setattr(pge_module.PgeSettings, "load", lambda _path=None: object())
    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")
    monkeypatch.setattr(reconcile_module, "import_statement", lambda _path: imported)

    _profile, proposals = sync_profile(repository, "home", apply=False)

    assert len(proposals) == 1
    assert not tuple(cache.rglob("*.pdf"))


def test_account_selection_rejects_config_and_accepts_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    repository = NamedProfileRepository(tmp_path)
    repository.save(
        "ev",
        AccountProfile((AccountEpoch(date(1970, 1, 1), Config(tariff="EV2-A")),)),
    )

    assert main(["info", "--account", "ev"]) == 0
    assert "EV2-A" in capsys.readouterr().out
    assert main(["--config", str(tmp_path / "config.toml"), "--account", "ev", "now"]) == 1


def test_explicit_config_stops_mqtt_from_reverting_to_a_default_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config`` must stay a stateless override for ``mqtt`` too.

    A default profile can be selected two independent ways: the CLI's own
    ``--account``/config-file precedence (``_pricing_context``), and
    ``MqttSettings.load``'s own fallback to the ``TARIFFKIT_ACCOUNT`` /
    ``TARIFFKIT_PROFILE`` environment variables. ``--config`` must win over
    both, not just the first.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TARIFFKIT_ACCOUNT", "ev")
    repository = NamedProfileRepository(tmp_path)
    repository.save(
        "ev",
        AccountProfile((AccountEpoch(date(1970, 1, 1), Config(tariff="EV2-A")),)),
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text('tariff = "E-ELEC"\n', encoding="utf-8")

    args = argparse.Namespace(
        config=config_path,
        account=None,
        broker="broker.local",
        port=None,
        username=None,
        topic_prefix=None,
        discovery=None,
        forecast_hours=None,
        tls=None,
    )
    _engine, config, profile_name, _repository = _pricing_context(args)

    assert profile_name is None  # --config alone must disable profile selection

    settings = _mqtt_settings(args, config=config, profile_name=profile_name)

    assert settings.profile is None
