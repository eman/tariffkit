"""`tariffkit setup`: the guided first run, and where it writes each answer."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tariffkit import secrets
from tariffkit.cli import main, setup
from tariffkit.cli.account_store import AccountStore
from tariffkit.config import default_config_path, default_dotenv_path


def scripted(answers: list[str], secrets_: list[str] | None = None) -> setup.Prompts:
    """Prompts that answer from lists, failing loudly if a question is unexpected."""
    replies: Iterator[str] = iter(answers)
    hidden: Iterator[str] = iter(secrets_ or [])

    def ask(prompt: str) -> str:
        try:
            return next(replies)
        except StopIteration:
            raise AssertionError(f"unexpected question: {prompt!r}") from None

    def secret(prompt: str) -> str:
        try:
            return next(hidden)
        except StopIteration:
            raise AssertionError(f"unexpected secret prompt: {prompt!r}") from None

    return setup.Prompts(ask=ask, secret=secret, say=lambda _line: None)


class TestWriteTomlValues:
    def test_creates_the_file_and_section(self, tmp_path: Path) -> None:
        path = tmp_path / "tariffkit" / "config.toml"

        setup.write_toml_values(path, "influxdb", {"host": "http://influx:8181", "port": 1})

        assert tomllib.loads(path.read_text())["influxdb"] == {
            "host": "http://influx:8181",
            "port": 1,
        }

    def test_keeps_comments_and_other_sections(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '# mine\ntariff = "E-ELEC"\n\n[mqtt]\nport = 1883\n\n[home_assistant]\nx = 1\n'
        )

        setup.write_toml_values(path, "mqtt", {"port": 8883, "tls": True})

        assert path.read_text() == (
            '# mine\ntariff = "E-ELEC"\n\n[mqtt]\nport = 8883\ntls = true\n\n'
            "[home_assistant]\nx = 1\n"
        )

    def test_quotes_strings_safely(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"

        setup.write_toml_values(path, "home_assistant", {"host": 'http://a"b\\c'})

        assert tomllib.loads(path.read_text())["home_assistant"]["host"] == 'http://a"b\\c'


class TestSettingsPrecedence:
    def test_a_value_already_in_dotenv_is_updated_there(self) -> None:
        secrets.set_dotenv_value("HA_HOST", "http://old:8123")
        settings = setup.Settings(default_config_path(), say=lambda _line: None)

        settings.save("home_assistant", {"host": ("http://new:8123", "HA_HOST")})

        assert secrets.load_dotenv()["HA_HOST"] == "http://new:8123"
        assert not default_config_path().exists()

    def test_a_value_pinned_by_the_environment_is_reported_not_shadowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_HOST", "http://pinned:8123")
        said: list[str] = []
        settings = setup.Settings(default_config_path(), say=said.append)

        settings.save("home_assistant", {"host": ("http://new:8123", "HA_HOST")})

        assert not default_config_path().exists()
        assert any("HA_HOST is set in your environment" in line for line in said)


def test_manual_account_is_dated_from_when_the_schedule_began(
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts = scripted(
        [
            "n",  # no PG&E login
            "manual",
            "e-tou-c",
            "n",  # no CCA
            "x",  # baseline territory
            "not a date",
            "2025-04-02",
            "2025",
            "",  # on this schedule since: the PTO date
            "none",  # no meter source
            "n",  # no MQTT
        ]
    )

    assert setup.run_setup(None, prompts) == 0

    epoch = AccountStore().load().epochs[0]
    assert epoch.effective.isoformat() == "2025-04-02"
    assert epoch.config.tariff == "E-TOU-C"
    assert epoch.config.baseline_territory == "X"
    assert epoch.config.interconnection_year == 2025
    # The closing check ran against what was just written.
    ready = capsys.readouterr().out.split("Set up")[1].split("Not set up")[0]
    assert "Account" in ready


def test_home_assistant_answers_land_where_the_loader_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        setup, "check_home_assistant", lambda host, token: checked.append((host, token))
    )
    monkeypatch.setattr(setup, "energy_counters", lambda host, token: ["sensor.grid_in"])
    prompts = scripted(
        [
            "n",  # no PG&E login
            "skip",  # no account
            "ha",
            "http://ha.lan:8123",
            "sensor.grid_in",
            "sensor.grid_out",
            "n",  # no MQTT
        ],
        ["long-lived-token"],
    )

    assert setup.run_setup(None, prompts) == 0

    from tariffkit.sources.homeassistant import HaSettings

    settings = HaSettings.load()
    assert checked == [("http://ha.lan:8123", "long-lived-token")]
    assert settings.host == "http://ha.lan:8123"
    assert settings.token == "long-lived-token"
    assert (settings.import_entity, settings.export_entity) == ("sensor.grid_in", "sensor.grid_out")
    # With no keyring, the token went to the owner-only .env, never config.toml.
    assert "long-lived-token" not in default_config_path().read_text()
    assert default_dotenv_path().stat().st_mode & 0o777 == 0o600


def test_mqtt_credentials_are_not_stored_where_they_could_not_load() -> None:
    prompts = scripted(
        [
            "n",
            "skip",
            "none",
            "y",  # MQTT
            "broker.lan",
            "",  # port 1883
            "n",  # no TLS
            "y",  # needs credentials
            "n",  # not a trusted network
        ]
    )

    assert setup.run_setup(None, prompts) == 0

    from tariffkit.mqtt import MqttSettings

    settings = MqttSettings.load()
    assert (settings.broker, settings.port, settings.tls) == ("broker.lan", 1883, False)
    assert settings.username is None
    assert "TARIFFKIT_MQTT_USERNAME" not in secrets.load_dotenv()


def test_interrupt_stops_cleanly() -> None:
    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    said: list[str] = []
    prompts = setup.Prompts(ask=interrupted, secret=interrupted, say=said.append)

    assert setup.run_setup(None, prompts) == 130
    assert any("setup stopped" in line for line in said)


def test_setup_is_a_cli_command(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Path | None] = []
    monkeypatch.setattr(setup, "run_setup", lambda config: ran.append(config) or 0)

    assert main(["setup"]) == 0
    assert ran == [None]


class _ChallengedSession:
    """A PgeSession stand-in partway through a device challenge."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.codes: list[str] = []

    def send_device_code(self, channel: str) -> None:
        self.sent.append(channel)

    def verify_device_code(self, code: str) -> tuple[str, str]:
        from tariffkit.sources.pge import PortalError

        self.codes.append(code)
        if code != "123456":
            raise PortalError("that code was not accepted; 2 attempts left")
        return "device-b", "device-v"


def test_an_unrecognised_device_is_verified_with_a_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tariffkit.sources.pge import DeviceNotRecognisedError

    session = _ChallengedSession()

    def check(_config: object, verify_device: Any) -> str | None:
        challenge = DeviceNotRecognisedError("new device", email="s***@x.com", phone="***-1234")
        verify_device(session, challenge)
        return None

    monkeypatch.setattr(setup, "check_pge", check)
    prompts = scripted(
        [
            "y",  # use a PG&E login
            "someone@example.com",
            "text",  # send the code by text message
            "000000",  # wrong
            "123456",
            "skip",  # no account
            "none",
            "n",
        ],
        ["hunter2"],
    )

    assert setup.run_setup(None, prompts) == 0

    assert session.sent == ["Phone"]
    assert session.codes == ["000000", "123456"]
    stored = secrets.load_dotenv()
    assert stored["PGE_BROWSER_COOKIE"] == "device-b"
    assert stored["PGE_VALIDATION_COOKIE"] == "device-v"


def test_an_address_without_a_scheme_is_tried_as_https_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried: list[str] = []

    def check(host: str, token: str) -> str | None:
        tried.append(host)
        return None if host.startswith("http://") else "could not reach: TLS handshake"

    monkeypatch.setattr(setup, "check_home_assistant", check)

    assert setup.resolve_home_assistant("ha.lan:8123", "t") == ("http://ha.lan:8123", None)
    assert tried == ["https://ha.lan:8123", "http://ha.lan:8123"]


def test_a_refused_token_still_settles_the_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup, "check_home_assistant", lambda host, token: "Home Assistant rejected the token (401)"
    )

    host, problem = setup.resolve_home_assistant("ha.lan", "bad")

    assert host == "https://ha.lan"
    assert problem is not None and "401" in problem


class TestInflux:
    def test_an_address_without_a_scheme_is_tried_as_https_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tried: list[str] = []

        def check(host: str, database: str, token: str) -> str | None:
            tried.append(host)
            return None if host.startswith("http://") else "could not reach InfluxDB: TLS"

        monkeypatch.setattr(setup, "check_influx", check)

        assert setup.resolve_influx("influx.lan:8181", "db", "t") == (
            "http://influx.lan:8181",
            None,
        )
        assert tried == ["https://influx.lan:8181", "http://influx.lan:8181"]

    def test_a_refused_query_still_settles_the_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            setup,
            "check_influx",
            lambda host, database, token: "InfluxDB refused the query (401): unauthorized",
        )

        host, problem = setup.resolve_influx("influx.lan", "db", "bad")

        assert host == "https://influx.lan"
        assert problem is not None and "401" in problem

    @pytest.mark.parametrize(
        ("columns", "expected_filter"),
        [
            (["time", "entity_id", "value", "unit_of_measurement"], "unit_of_measurement = 'kWh'"),
            (["time", "entity_id", "value"], "entity_id LIKE '%energy%'"),
        ],
    )
    def test_counters_are_filtered_by_unit_where_the_table_records_one(
        self, monkeypatch: pytest.MonkeyPatch, columns: list[str], expected_filter: str
    ) -> None:
        import tariffkit.sources.influx as influx

        asked: list[str] = []

        def query(_settings: object, sql: str) -> list[dict[str, str]]:
            asked.append(sql)
            if "information_schema" in sql:
                return [{"column_name": column} for column in columns]
            return [{"entity_id": "grid_in"}, {"entity_id": "grid_out"}]

        monkeypatch.setattr(influx, "_query", query)

        assert setup.influx_counters("http://i", "db", "t") == ["grid_in", "grid_out"]
        assert expected_filter in asked[-1]

    def test_answers_land_where_the_loader_reads_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(setup, "check_influx", lambda host, database, token: None)
        monkeypatch.setattr(setup, "influx_counters", lambda host, database, token: ["grid_in"])
        prompts = scripted(
            [
                "n",  # no PG&E login
                "skip",  # no account
                "influx",
                "http://influx.lan:8181",
                "homedb",
                "grid_in",
                "grid_out",
                "n",  # no MQTT
            ],
            ["influx-token"],
        )

        assert setup.run_setup(None, prompts) == 0

        from tariffkit.sources.influx import InfluxSettings

        settings = InfluxSettings.load()
        assert (settings.host, settings.database, settings.token) == (
            "http://influx.lan:8181",
            "homedb",
            "influx-token",
        )
        assert (settings.import_entity, settings.export_entity) == ("grid_in", "grid_out")
