"""CLI, MQTT discovery, and the REST API."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tariffkit import Config, RateEngine
from tariffkit.account import AccountEpoch, AccountProfile, NamedProfileRepository
from tariffkit.cli import main
from tariffkit.mqtt.discovery import discovery_payloads


class TestCli:
    def test_now_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["now", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["export"]["vintage"] == "NBT26"
        assert "import" in payload and "spread" in payload

    def test_now_human_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["now"]) == 0
        out = capsys.readouterr().out
        assert "import" in out and "export" in out and "$/kWh" in out

    def test_forecast_table_marks_the_best_hour(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["forecast", "--hours", "12", "--start", "2026-09-15T12:00-07:00"]) == 0
        out = capsys.readouterr().out
        assert "0.60385" in out  # the 19:00 export credit
        assert "highest export credit" in out

    def test_forecast_csv(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert (
            main(
                ["forecast", "--hours", "3", "--start", "2026-09-15T12:00-07:00", "--format", "csv"]
            )
            == 0
        )
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("start,end,import,export,spread")
        assert len(lines) == 4

    def test_forecast_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["forecast", "--hours", "5", "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["hours"] == 5

    def test_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["info"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["export_vintage"] == "NBT26"
        assert payload["daily_fixed_charge"] == pytest.approx(0.79343)

    def test_library_errors_exit_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["forecast", "--hours", "2", "--start", "2060-01-01T00:00-08:00"])
        assert code == 1
        assert "error:" in capsys.readouterr().err


class TestDiscovery:
    @pytest.fixture
    def payloads(self) -> list[tuple[str, dict[str, Any]]]:
        return discovery_payloads(RateEngine(Config()).describe())

    def test_covers_every_sensor(self, payloads: list[tuple[str, dict[str, Any]]]) -> None:
        keys = {topic.split("/")[-2] for topic, _ in payloads}
        assert keys == {"import_price", "export_price", "spread", "tou_period"}

    def test_topics_are_under_the_discovery_prefix(
        self, payloads: list[tuple[str, dict[str, Any]]]
    ) -> None:
        assert all(topic.startswith("homeassistant/sensor/tariffkit/") for topic, _ in payloads)

    def test_price_sensors_declare_a_unit_but_not_a_monetary_device_class(
        self, payloads: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Home Assistant rejects monetary sensors whose unit is not a currency."""
        for topic, payload in payloads:
            if topic.endswith("tou_period/config"):
                continue
            assert payload["unit_of_measurement"] == "USD/kWh"
            assert "device_class" not in payload

    def test_entities_share_one_device_and_an_availability_topic(
        self, payloads: list[tuple[str, dict[str, Any]]]
    ) -> None:
        assert {p["device"]["identifiers"][0] for _, p in payloads} == {"tariffkit"}
        assert {p["device"]["manufacturer"] for _, p in payloads} == {
            "Pacific Gas and Electric Company"
        }
        assert {p["device"]["name"] for _, p in payloads} == {"PG&E Rates"}
        assert all(p["availability_topic"] == "tariffkit/status" for _, p in payloads)

    def test_unique_ids_are_distinct(self, payloads: list[tuple[str, dict[str, Any]]]) -> None:
        ids = [p["unique_id"] for _, p in payloads]
        assert len(ids) == len(set(ids))

    def test_custom_prefix(self) -> None:
        payloads = discovery_payloads(RateEngine(Config()).describe(), topic_prefix="energy/pge")
        assert all(p["state_topic"].startswith("energy/pge/") for _, p in payloads)


class TestWebApi:
    @pytest.fixture
    def client(self) -> Any:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from tariffkit.web import create_app

        return fastapi_testclient.TestClient(create_app(Config()))

    def test_healthz(self, client: Any) -> None:
        assert client.get("/v1/healthz").json() == {"status": "ok"}

    def test_meta(self, client: Any) -> None:
        assert client.get("/v1/meta").json()["export_vintage"] == "NBT26"

    def test_price_now(self, client: Any) -> None:
        body = client.get("/v1/price/now").json()
        assert set(body) >= {"start", "end", "import", "export", "spread"}

    def test_price_at(self, client: Any) -> None:
        body = client.get("/v1/price/at", params={"ts": "2026-09-15T19:00:00-07:00"}).json()
        assert body["export"]["total"] == pytest.approx(0.60385)
        assert body["import"]["total"] == pytest.approx(0.55214)

    def test_price_at_rejects_naive_timestamps(self, client: Any) -> None:
        assert client.get("/v1/price/at", params={"ts": "2026-09-15T19:00:00"}).status_code == 422

    def test_price_outside_coverage_is_404(self, client: Any) -> None:
        response = client.get("/v1/price/at", params={"ts": "2060-01-01T00:00:00-08:00"})
        assert response.status_code == 404

    def test_forecast(self, client: Any) -> None:
        body = client.get("/v1/forecast", params={"hours": 6}).json()
        assert body["hours"] == 6
        assert len(body["points"]) == 6

    def test_forecast_rejects_absurd_horizons(self, client: Any) -> None:
        assert client.get("/v1/forecast", params={"hours": 0}).status_code == 422
        assert client.get("/v1/forecast", params={"hours": 10**6}).status_code == 422

    def test_post_pricing_can_select_an_existing_profile(self, tmp_path: Path) -> None:
        from tariffkit.web import create_app

        repository = NamedProfileRepository(tmp_path)
        repository.save(
            "ev",
            AccountProfile((AccountEpoch(date(1970, 1, 1), Config(tariff="EV2-A")),)),
        )
        client = self._client(create_app(Config(), profile_repository=repository))

        response = client.post("/v1/meta", json={"profile": "ev"})

        assert response.status_code == 200
        assert response.json()["account_profile"] == "ev"

    def test_unknown_profile_does_not_disclose_profile_storage(self, tmp_path: Path) -> None:
        from tariffkit.web import create_app

        repository = NamedProfileRepository(tmp_path)
        client = self._client(create_app(Config(), profile_repository=repository))

        response = client.post("/v1/price/now", json={"profile": "missing"})

        assert response.status_code == 404
        assert response.json()["detail"] == "profile unavailable"

    def test_post_without_config_stays_invalid_without_a_default_profile(self, client: Any) -> None:
        assert client.post("/v1/price/now", json={}).status_code == 422

    def test_post_rejects_mutation_and_credential_fields(self, client: Any) -> None:
        assert client.post("/v1/price/now", json={"credentials": {}}).status_code == 422
        assert client.post("/v1/price/now", json={"pdf": "statement.pdf"}).status_code == 422

    def test_profile_prehistory_is_a_typed_not_found(self, tmp_path: Path) -> None:
        from tariffkit.web import create_app

        repository = NamedProfileRepository(tmp_path)
        repository.save(
            "future",
            AccountProfile((AccountEpoch(date(2030, 1, 1), Config()),)),
        )
        client = self._client(create_app(profile_name="future", profile_repository=repository))

        response = client.get("/v1/price/at", params={"ts": "2026-09-15T19:00:00-07:00"})

        assert response.status_code == 404
        assert "before the first account epoch" in response.json()["detail"]

    def test_server_uses_configured_default_profile(self, tmp_path: Path) -> None:
        from tariffkit.web import create_app

        repository = NamedProfileRepository(tmp_path)
        repository.save(
            "ev",
            AccountProfile((AccountEpoch(date(1970, 1, 1), Config(tariff="EV2-A")),)),
        )
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[account]\ndefault_profile = "ev"\ntariff = "E-ELEC"\n', encoding="utf-8"
        )
        client = self._client(create_app(profile_repository=repository, config_path=config_path))

        assert client.get("/v1/meta").json()["account_profile"] == "ev"

    @staticmethod
    def _client(app: Any) -> Any:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        return fastapi_testclient.TestClient(app)
