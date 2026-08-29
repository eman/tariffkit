"""Persistent settings, keyring credentials, and request-scoped configuration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tariffkit import Config, Supplier, secrets
from tariffkit.cli import main
from tariffkit.errors import ConfigError
from tariffkit.mqtt.publisher import MqttSettings
from tariffkit.sources.homeassistant import HaSettings
from tariffkit.sources.influx import InfluxSettings
from tariffkit.sources.pge import PgeSettings


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.errors = SimpleNamespace(KeyringError=RuntimeError)

    def get_password(self, service: str, name: str) -> str | None:
        return self.values.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.values[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.values[(service, name)]


def test_config_round_trips_through_api_shape() -> None:
    original = Config.from_dict(
        {
            "tariff": "E-TOU-C",
            "supplier": "cca",
            "interconnection_year": 2025,
            "pto_date": "2025-04-02",
            "baseline_territory": "X",
            "cca": {"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011},
        }
    )

    assert Config.from_dict(original.to_dict()) == original


def test_environment_can_express_complete_cca_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARIFFKIT_SUPPLIER", "cca")
    monkeypatch.setenv("TARIFFKIT_TARIFF", "EV2-A")
    monkeypatch.setenv("TARIFFKIT_PTO_DATE", "2025-04-02")
    monkeypatch.setenv(
        "TARIFFKIT_CCA_JSON",
        json.dumps({"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011}),
    )

    config = Config.from_env()

    assert config.supplier is Supplier.CCA
    assert config.tariff == "EV2-A"
    assert config.cca is not None and config.cca.rate_card == "mce"


def test_invalid_cca_environment_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARIFFKIT_CCA_JSON", "[]")
    with pytest.raises(ConfigError, match="JSON object"):
        Config.from_env()


def test_keyring_never_lists_values(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeKeyring()
    monkeypatch.delenv("TARIFFKIT_DISABLE_KEYRING")
    monkeypatch.setattr(secrets, "_keyring", lambda: backend)

    secrets.set_secret("pge.password", "not-printed")

    assert secrets.get_secret("pge.password") == "not-printed"
    assert secrets.configured_secrets() == ("pge.password",)
    secrets.delete_secret("pge.password")
    assert secrets.configured_secrets() == ()


def test_cli_prompts_instead_of_accepting_secret_on_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr("tariffkit.cli.getpass.getpass", lambda prompt: "hidden")
    monkeypatch.setattr(
        "tariffkit.cli.set_secret", lambda name, value: stored.append((name, value))
    )

    assert main(["credentials", "set", "pge.password"]) == 0
    assert stored == [("pge.password", "hidden")]
    assert "hidden" not in capsys.readouterr().out


def test_mqtt_settings_resolve_config_environment_and_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[mqtt]\nbroker = "config-broker"\nport = 1884\ntls = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TARIFFKIT_MQTT_BROKER", "env-broker")
    monkeypatch.setattr(
        "tariffkit.mqtt.publisher.get_secret",
        lambda name: {"mqtt.username": "user", "mqtt.password": "pass"}.get(name),
    )

    settings = MqttSettings.load(config, tmp_path / "absent")

    assert (settings.broker, settings.port) == ("env-broker", 1884)
    assert (settings.username, settings.password) == ("user", "pass")


def test_source_credentials_fall_back_to_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[home_assistant]\nhost = "https://ha.example"\n'
        '[influxdb]\nhost = "influx.example"\ndatabase = "energy"\n',
        encoding="utf-8",
    )
    values = {
        "home_assistant.token": "ha-secret",
        "influxdb.token": "influx-secret",
        "pge.username": "person@example.invalid",
        "pge.password": "pge-secret",
    }
    for name in (
        "HA_HOST",
        "HA_TOKEN",
        "INFLUXDB3_HOST",
        "INFLUXDB3_DATABASE",
        "INFLUXDB3_AUTH_TOKEN",
        "PGE_USERNAME",
        "PGE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    for module in (
        "tariffkit.sources.homeassistant.get_secret",
        "tariffkit.sources.influx.get_secret",
        "tariffkit.sources.pge.get_secret",
    ):
        monkeypatch.setattr(module, values.get)

    ha = HaSettings.load(config, tmp_path / "absent")
    influx = InfluxSettings.load(config, tmp_path / "absent")
    pge = PgeSettings.load(config, tmp_path / "absent")

    assert ha.token == "ha-secret"
    assert influx.token == "influx-secret"
    assert pge.username == "person@example.invalid"
    assert repr(pge).find("pge-secret") == -1


class TestConfiguredWebApi:
    @pytest.fixture
    def client(self) -> Any:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from tariffkit.web import create_app

        return fastapi_testclient.TestClient(create_app(Config()))

    def test_request_config_changes_the_tariff(self, client: Any) -> None:
        response = client.post(
            "/v1/price/at",
            json={
                "ts": "2026-09-15T19:00:00-07:00",
                "config": Config(tariff="EV2-A").to_dict(),
            },
        )

        assert response.status_code == 200
        assert response.json()["import"]["total"] == pytest.approx(0.53809)

    def test_request_config_rejects_credentials(self, client: Any) -> None:
        response = client.post(
            "/v1/price/now",
            json={"config": {"tariff": "E-ELEC", "password": "must-not-be-accepted"}},
        )

        assert response.status_code == 422
        assert "unknown config keys" in response.json()["detail"]

    def test_request_config_requires_offset(self, client: Any) -> None:
        response = client.post(
            "/v1/forecast",
            json={"config": Config().to_dict(), "start": "2026-09-15T19:00:00", "hours": 2},
        )
        assert response.status_code == 422


class TestCredentialsStayPrivate:
    """Secrets must not leak through reprs or a world-readable cache."""

    def test_settings_reprs_hide_their_secrets(self) -> None:
        from tariffkit.mqtt.publisher import MqttSettings
        from tariffkit.sources.homeassistant import HaSettings
        from tariffkit.sources.influx import InfluxSettings

        rendered = " ".join(
            (
                repr(HaSettings(host="http://h", token="HA-SECRET")),
                repr(InfluxSettings(host="http://i", database="d", token="INFLUX-SECRET")),
                repr(MqttSettings(broker="h", username="u", password="MQTT-SECRET", tls=True)),
            )
        )
        assert "SECRET" not in rendered

    def test_the_cookie_cache_is_not_relative_to_the_working_directory(self) -> None:
        from tariffkit.sources.pge import _default_cookie_path

        assert _default_cookie_path().is_absolute()

    def test_an_existing_cookie_file_is_reduced_to_0600(self, tmp_path: Path) -> None:
        """os.open's mode applies only on creation; fchmod covers the rest."""
        from tariffkit.sources.pge import PgeSession, PgeSettings

        path = tmp_path / "cookies.json"
        path.write_text("[]", encoding="utf-8")
        path.chmod(0o644)

        session = PgeSession(PgeSettings(username="u", password="p", cookie_path=path))
        session._client = httpx.Client()
        try:
            session._save_cookies()
        finally:
            session._client.close()

        assert path.stat().st_mode & 0o777 == 0o600
