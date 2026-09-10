"""Focused tests for named-account CLI maintenance."""

from __future__ import annotations

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
    ObservedAgreement,
)
from tariffkit.billing import BillingPeriod, IntervalReading
from tariffkit.cli.account_commands import migrate_existing, sync_profile
from tariffkit.cli.account_store import AccountStore
from tariffkit.cli.commands import _mqtt_settings, build_parser, main
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

    profile = migrate_existing(config_path=config)

    assert profile.epochs[0].config.tariff == "EV2-A"


def test_account_migration_requires_explicit_existing_audit_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="legacy audit account file not found"):
        migrate_existing(audit_path=tmp_path / "missing.toml")


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
    assert main(["account", "export", "--output", str(exported)]) == 0
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
                    "--effective",
                    "2026-01-01",
                    "--tariff",
                    "EV2-A",
                ]
            )
            == 0
        )
        assert [epoch.effective for epoch in AccountStore(tmp_path).load().epochs] == [
            date(2025, 1, 1)
        ]
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
                "--effective",
                "2025-01-01",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = AccountStore(tmp_path)
    assert store.load().meter_sources == MeterSources()

    assert (
        main(
            [
                "account",
                "source",
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
    assert store.load().meter_sources == MeterSources()

    assert (
        main(
            [
                "account",
                "source",
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
    assert store.load().meter_sources.ha == MeterSource("sensor.grid_in", "sensor.grid_out")
    capsys.readouterr()
    assert main(["account", "source", "show", "ha", "--json"]) == 0
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
    store = AccountStore(tmp_path)
    store.save(profile)

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
    store = AccountStore(tmp_path)
    store.save(
        AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)),
    )
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF synthetic")
    imported = observation(tariff="EV2-A", digest="a" * 64)

    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")

    monkeypatch.setattr(reconcile_module, "import_statement", lambda _path: imported)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert main(["account", "import-statement", str(pdf), "--json"]) == 0
    assert len(store.load().epochs) == 1
    assert main(["account", "import-statement", str(pdf), "--apply"]) == 0
    assert [epoch.config.tariff for epoch in store.load().epochs] == [
        "E-ELEC",
        "EV2-A",
    ]


def test_account_sync_removes_private_cache_after_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AccountStore(tmp_path)
    store.save(
        AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)),
    )
    imported = observation(tariff="EV2-A", digest="b" * 64)
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    class Session:
        def __init__(self) -> None:
            self.signed_in = False

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def login(self, *, force: bool = False) -> None:
            self.signed_in = True

        def bill_history(self) -> list[dict[str, str]]:
            # The portal lists nothing for a session that never minted a CSRF
            # token, which is what a resumed session is until `login()` runs.
            if not self.signed_in:
                return []
            return [{"billId": "bill-1", "billDate": "2026-02-01"}]

        def download_bill(self, _bill_id: str) -> bytes:
            return b"%PDF synthetic"

    import tariffkit.sources.pge as pge_module

    opened: list[Session] = []

    def _session(_settings: object) -> Session:
        opened.append(Session())
        return opened[-1]

    monkeypatch.setattr(pge_module, "PgeSession", _session)
    monkeypatch.setattr(pge_module.PgeSettings, "load", lambda _path=None: object())
    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")
    monkeypatch.setattr(reconcile_module, "import_statement", lambda _path: imported)

    _profile, proposals, skipped = sync_profile(store, apply=False)

    assert len(proposals) == 1
    assert skipped == []
    assert not tuple(cache.rglob("*.pdf"))
    # Without this the statement list comes back empty and the sync reports
    # "0 statement update(s)" against an account that has plenty.
    assert opened[0].signed_in, "sync must sign in before listing statements"


def test_one_unreadable_statement_does_not_discard_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal lists what it lists; the parser does not have to like all of it.

    A `StatementError` from one document used to propagate out of the whole
    loop, so an account with statements going back years imported none of them
    because the newest one would not parse -- and the command exited non-zero,
    as though the portal or the credentials were at fault. Reported from a real
    sync as `error: statement-0000.pdf: no total amount due found`, which named
    a temporary file inside a cache directory the function deletes on its way
    out: nothing the owner could open, and no clue which statement it meant.
    """
    from tariffkit.providers.pge.statements.errors import StatementError

    store = AccountStore(tmp_path)
    store.save(AccountProfile((AccountEpoch(date(2025, 1, 1), Config()),)))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def login(self, *, force: bool = False) -> None:
            return None

        def bill_history(self) -> list[dict[str, str]]:
            return [
                {"billId": "newest", "billDate": "2026-09-04"},
                {"billId": "older", "billDate": "2026-08-04"},
            ]

        def download_bill(self, bill_id: str) -> bytes:
            return bill_id.encode()

    import tariffkit.sources.pge as pge_module

    monkeypatch.setattr(pge_module, "PgeSession", lambda _settings: Session())
    monkeypatch.setattr(pge_module.PgeSettings, "load", lambda _path=None: object())

    good = observation(tariff="EV2-A", digest="c" * 64)

    def _import(path: Path) -> object:
        if path.read_bytes() == b"newest":
            raise StatementError(f"{path.name}: no total amount due found")
        return good

    reconcile_module = importlib.import_module("tariffkit.providers.pge.reconcile")
    monkeypatch.setattr(reconcile_module, "import_statement", _import)

    _profile, proposals, skipped = sync_profile(store, apply=False)

    assert len(proposals) == 1, "the readable statement still imported"
    assert len(skipped) == 1
    # Named by the date the utility issued it, not by the temporary file the
    # loop index produced -- that name is gone by the time anyone reads it.
    assert skipped[0]["statement"] == "2026-09-04"
    assert skipped[0]["reason"] == "no total amount due found"
    assert "statement-0000" not in skipped[0]["reason"]


def test_mqtt_cli_accepts_insecure_auth_escape_hatch() -> None:
    args = build_parser().parse_args(
        ["mqtt", "--broker", "broker.local", "--username", "user", "--allow-insecure-auth"]
    )

    settings = _mqtt_settings(args, from_account=False)

    assert settings.allow_insecure_auth is True
