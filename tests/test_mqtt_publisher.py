"""Publisher behaviour, exercised against a fake client rather than a broker."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tariffkit import CcaConfig, Config, RateEngine, Supplier
from tariffkit.account import AccountEpoch, AccountProfile, NamedProfileRepository
from tariffkit.components import EXPORT_GROUPS, IMPORT_GROUPS
from tariffkit.errors import ConfigError
from tariffkit.mqtt.publisher import OFFLINE, ONLINE, MqttPublisher, MqttSettings
from tariffkit.timeutil import PACIFIC


class FakeClient:
    """Records everything published, in order."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.will: tuple[str, str, bool] | None = None
        self.connected: tuple[str, int] | None = None
        self.auth: tuple[str, str | None] | None = None
        self.tls = False
        self.loop_running = False
        self.disconnected = False

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.auth = (username, password)

    def tls_set(self) -> None:
        self.tls = True

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


def test_anonymous_plaintext_connection_is_allowed() -> None:
    publisher = make_publisher()

    assert client_of(publisher).auth is None
    assert client_of(publisher).tls is False


def test_authenticated_plaintext_connection_is_rejected() -> None:
    with pytest.raises(ConfigError, match="credentials require TLS"):
        make_publisher(username="user")


def test_password_without_username_is_rejected_even_with_tls() -> None:
    with pytest.raises(ConfigError, match="password requires a username"):
        make_publisher(password="secret", tls=True)


def test_insecure_auth_escape_hatch_must_be_boolean() -> None:
    with pytest.raises(ConfigError, match="allow_insecure_auth must be a boolean"):
        make_publisher(username="user", allow_insecure_auth="false")


def test_authenticated_tls_connection_configures_auth_and_tls() -> None:
    publisher = make_publisher(username="user", password="secret", tls=True, port=8883)

    client = client_of(publisher)
    assert client.auth == ("user", "secret")
    assert client.tls is True


def test_explicit_insecure_auth_allows_credentials_without_tls() -> None:
    publisher = make_publisher(
        username="user",
        password="secret",
        allow_insecure_auth=True,
    )

    client = client_of(publisher)
    assert client.auth == ("user", "secret")
    assert client.tls is False


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


def test_component_topics_stack_to_the_price(publisher: MqttPublisher) -> None:
    """A dashboard stacking the group topics must land on the price topic."""
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    topics = client_of(publisher).topics()

    for direction, groups in (("import", IMPORT_GROUPS), ("export", EXPORT_GROUPS)):
        stack = sum(float(topics[f"tariffkit/components/{direction}/{group}"]) for group in groups)
        assert stack == pytest.approx(float(topics[f"tariffkit/{direction}_price"]), abs=5e-5)

    payload = json.loads(topics["tariffkit/components/export/generation/attributes"])
    assert payload["components"] == {"generation": 0.59312}
    # A band is a slice of its price, so it repeats that price's quality flags
    # rather than making a subscriber correlate two topics to trust a number.
    assert payload["complete"] is True
    assert payload["locked"] is True
    assert payload["exact"] is True

    imported = json.loads(topics["tariffkit/components/import/generation/attributes"])
    # A retail schedule is published, not vintaged, so the import side has only
    # the one flag -- the same set its own price topic carries.
    assert set(imported) == {"components", "complete"}


def test_component_topics_carry_an_incomplete_flag() -> None:
    """The flag has to track the price, not be published as a constant true."""
    engine = RateEngine(Config(supplier=Supplier.CCA, cca=CcaConfig(name="Unconfigured CCA")))
    publisher = MqttPublisher(engine, MqttSettings(broker="broker.local"), client=FakeClient())
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    topics = client_of(publisher).topics()

    # No CCA generation rate configured: the price is delivery-only and says so,
    # and every band of it has to say so too.
    assert json.loads(topics["tariffkit/export_price/attributes"])["complete"] is False
    for group in EXPORT_GROUPS:
        payload = json.loads(topics[f"tariffkit/components/export/{group}/attributes"])
        assert payload["complete"] is False, group


def test_daily_fixed_charge_is_published_per_day(publisher: MqttPublisher) -> None:
    """Published beside the prices, never inside them."""
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    topics = client_of(publisher).topics()

    assert float(topics["tariffkit/daily_fixed_charge"]) > 0
    payload = json.loads(topics["tariffkit/import_price/attributes"])
    assert "base_services_charge" not in payload["components"]


def test_attributes_carry_the_predbat_rate_lists(publisher: MqttPublisher) -> None:
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/import_price/attributes"])

    assert len(payload["raw_today"]) == 48  # a full calendar day, not from 19:00
    assert len(payload["raw_tomorrow"]) == 48
    assert set(payload["raw_today"][0]) == {"from", "to", "rate"}
    assert payload["raw_today"][0]["from"].endswith("T00:00:00-07:00")


def test_predbat_values_are_cents(publisher: MqttPublisher) -> None:
    """Predbat assumes pence, so dollars would be off by 100x against its defaults."""
    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
    payload = json.loads(client_of(publisher).topics()["tariffkit/export_price/attributes"])

    at_seven_pm = [e for e in payload["raw_today"] if e["from"].endswith("T19:00:00-07:00")]
    assert at_seven_pm[0]["rate"] == 60.385  # the state topic publishes 0.60385


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


def test_selected_profile_drives_active_rates(tmp_path: Path) -> None:
    repository = NamedProfileRepository(tmp_path)
    repository.save(
        "ev",
        AccountProfile((AccountEpoch(date(1970, 1, 1), Config(tariff="EV2-A")),)),
    )
    settings = MqttSettings(broker="broker.local", profile="ev")
    publisher = MqttPublisher(
        RateEngine(Config()),
        settings,
        client=FakeClient(),
        profile_repository=repository,
    )

    publisher.publish_now(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))

    assert publisher.engine.describe()["account_profile"] == "ev"


def test_mqtt_settings_read_default_profile_from_shared_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[account]\ndefault_profile = "home"\n[mqtt]\nbroker = "broker.local"\n',
        encoding="utf-8",
    )

    settings = MqttSettings.load(config, tmp_path / "absent")

    assert settings.profile == "home"


def test_mqtt_settings_loads_insecure_auth_escape_hatch_from_toml(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[mqtt]\nbroker = "broker.local"\nallow_insecure_auth = true\n',
        encoding="utf-8",
    )

    settings = MqttSettings.load(
        config,
        tmp_path / "absent",
        username="user",
        password="secret",
    )

    assert settings.allow_insecure_auth is True


def test_mqtt_settings_loads_insecure_auth_escape_hatch_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TARIFFKIT_MQTT_BROKER", "broker.local")
    monkeypatch.setenv("TARIFFKIT_MQTT_USERNAME", "user")
    monkeypatch.setenv("TARIFFKIT_MQTT_ALLOW_INSECURE_AUTH", "true")

    settings = MqttSettings.load(tmp_path / "absent", tmp_path / "absent-dotenv")

    assert settings.allow_insecure_auth is True


def test_mqtt_settings_rejects_invalid_insecure_auth_environment_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TARIFFKIT_MQTT_ALLOW_INSECURE_AUTH", "sometimes")

    with pytest.raises(ConfigError, match="must be a boolean"):
        MqttSettings.load(
            tmp_path / "absent",
            tmp_path / "absent-dotenv",
            broker="broker.local",
        )


def test_mqtt_environment_profile_overrides_config_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[mqtt]\nbroker = "broker.local"\naccount = "old-account"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TARIFFKIT_ACCOUNT", "new-account")

    settings = MqttSettings.load(config, tmp_path / "absent")

    assert settings.profile == "new-account"
    assert settings.account is None


def test_mqtt_settings_accepts_account_alias() -> None:
    assert MqttSettings(broker="broker.local", account="home").profile == "home"


def test_mqtt_settings_rejects_conflicting_profile_aliases() -> None:
    with pytest.raises(ConfigError, match="must select the same profile"):
        MqttSettings.load(
            broker="broker.local",
            account="home",
            profile_name="other",
        )


def test_mqtt_settings_collapses_matching_profile_aliases() -> None:
    settings = MqttSettings.load(
        broker="broker.local",
        account="home",
        profile_name="home",
    )

    assert settings.profile == "home"
