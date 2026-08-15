"""Publisher behaviour, exercised against a fake client rather than a broker."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from tariffkit import Config, RateEngine
from tariffkit.mqtt.publisher import OFFLINE, ONLINE, MqttPublisher, MqttSettings
from tariffkit.timeutil import PACIFIC


class FakeClient:
    """Records everything published, in order."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.will: tuple[str, str, bool] | None = None
        self.connected: tuple[str, int] | None = None
        self.loop_running = False
        self.disconnected = False

    def will_set(self, topic: str, payload: str, retain: bool = False) -> None:
        self.will = (topic, payload, retain)

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected = (host, port)

    def loop_start(self) -> None:
        self.loop_running = True

    def loop_stop(self) -> None:
        self.loop_running = False

    def disconnect(self) -> None:
        self.disconnected = True

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))

    def topics(self) -> dict[str, str]:
        return {topic: payload for topic, payload, _ in self.published}


def make_publisher(**settings: Any) -> MqttPublisher:
    """Inject a fake client so `_configure` still runs for real."""
    settings.setdefault("broker", "broker.local")
    return MqttPublisher(RateEngine(Config()), MqttSettings(**settings), client=FakeClient())


@pytest.fixture
def publisher() -> MqttPublisher:
    return make_publisher()


def client_of(publisher: MqttPublisher) -> FakeClient:
    return publisher._client


def test_last_will_marks_the_device_offline(publisher: MqttPublisher) -> None:
    """Without this, a crashed publisher leaves stale prices looking live."""
    assert client_of(publisher).will == ("tariffkit/status", OFFLINE, True)


def test_connect_announces_online_and_publishes_discovery(publisher: MqttPublisher) -> None:
    publisher.connect()
    client = client_of(publisher)
    assert client.connected == ("broker.local", 1883)
    assert client.topics()["tariffkit/status"] == ONLINE
    assert any(t.startswith("homeassistant/sensor/") for t in client.topics())


def test_discovery_can_be_disabled() -> None:
    publisher = make_publisher(discovery=False)
    publisher.connect()
    assert not any(t.startswith("homeassistant/") for t in client_of(publisher).topics())


def test_publishes_known_values_retained(publisher: MqttPublisher) -> None:
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    topics = client_of(publisher).topics()

    assert topics["tariffkit/import_price"] == "0.55214"
    assert topics["tariffkit/export_price"] == "0.60385"
    # A bare number: Home Assistant will not parse a leading "+" as numeric.
    assert topics["tariffkit/spread"] == "0.05171"
    assert topics["tariffkit/tou_period"] == "peak"
    # Retained, so a subscriber connecting mid-hour gets the price immediately.
    assert all(retain for _, _, retain in client_of(publisher).published)


def test_attributes_carry_the_component_breakdown(publisher: MqttPublisher) -> None:
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/export_price/attributes"])
    assert payload["components"]["acc_plus"] == 0.0088
    assert payload["vintage"] == "NBT26"
    assert payload["locked"] is True


def test_attributes_carry_the_predbat_rate_lists(publisher: MqttPublisher) -> None:
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/import_price/attributes"])

    assert len(payload["raw_today"]) == 48  # a full calendar day, not from 19:00
    assert len(payload["raw_tomorrow"]) == 48
    assert set(payload["raw_today"][0]) == {"start", "end", "value"}
    assert payload["raw_today"][0]["start"].endswith("T00:00:00-07:00")


def test_predbat_values_are_cents(publisher: MqttPublisher) -> None:
    """Predbat assumes pence, so dollars would be off by 100x against its defaults."""
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/export_price/attributes"])

    at_seven_pm = [e for e in payload["raw_today"] if e["start"].endswith("T19:00:00-07:00")]
    assert at_seven_pm[0]["value"] == 60.385  # the state topic publishes 0.60385


def test_attributes_carry_the_emhass_series(publisher: MqttPublisher) -> None:
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    topics = client_of(publisher).topics()
    imports = json.loads(topics["tariffkit/import_price/attributes"])
    exports = json.loads(topics["tariffkit/export_price/attributes"])

    assert "load_cost_forecast" in imports and "prod_price_forecast" not in imports
    assert "prod_price_forecast" in exports and "load_cost_forecast" not in exports
    # Bare lists, not timestamped maps: EMHASS reads them positionally.
    assert isinstance(imports["load_cost_forecast"], list)
    # Dollars, unlike the Predbat lists alongside them.
    assert imports["load_cost_forecast"][0] == 0.55214
    assert exports["prod_price_forecast"][0] == 0.60385
    # 48 hours at EMHASS's 30-minute default, and the horizon travels with them.
    assert imports["prediction_horizon"] == len(imports["load_cost_forecast"]) == 96


def test_emhass_series_aligns_to_an_off_hour_publish(publisher: MqttPublisher) -> None:
    """`--once` from cron lands at an arbitrary minute, not on the hour."""
    publisher.publish_now(datetime(2026, 9, 15, 19, 45, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/import_price/attributes"])

    # The 19:00-19:30 slot has already elapsed, so the list starts at 19:30.
    assert payload["prediction_horizon"] == 95
    assert len(payload["load_cost_forecast"]) == 95


def test_forecast_attribute_is_a_flat_hourly_list(publisher: MqttPublisher) -> None:
    """Planners such as EMHASS consume this shape directly."""
    publisher.publish_now(datetime(2026, 9, 15, 12, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/spread/attributes"])
    forecast: list[dict[str, Any]] = payload["forecast"]
    assert len(forecast) == 48
    assert set(forecast[0]) == {"start", "import", "export", "spread"}
    assert max(f["export"] for f in forecast) == pytest.approx(0.60385)


def test_close_marks_offline_and_disconnects(publisher: MqttPublisher) -> None:
    publisher.connect()
    publisher.close()
    client = client_of(publisher)
    assert client.published[-1] == ("tariffkit/status", OFFLINE, True)
    assert client.disconnected and not client.loop_running


def test_custom_topic_prefix() -> None:
    publisher = make_publisher(topic_prefix="energy/pge")
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    assert "energy/pge/import_price" in client_of(publisher).topics()
