"""Publish current and forecast prices to an MQTT broker."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any

from ..account import (
    AccountProfile,
    AccountRateEngine,
    NamedProfileRepository,
    configured_profile_name,
)
from ..components import EXPORT_GROUPS, IMPORT_GROUPS, split_components
from ..config import default_config_path
from ..engine import RateEngine
from ..errors import ConfigError
from ..interop import forecast_lists, predbat_payload
from ..models import PricePoint
from ..secrets import get_secret
from ..sources.homeassistant import load_dotenv
from ..timeutil import next_hour, now_pacific
from .discovery import discovery_payloads

log = logging.getLogger(__name__)

ONLINE = "online"
OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class MqttSettings:
    broker: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "tariffkit"
    discovery: bool = True
    discovery_prefix: str = "homeassistant"
    forecast_hours: int = 48
    client_id: str = "tariffkit"
    tls: bool = False
    allow_insecure_auth: bool = False
    profile: str | None = None
    account: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tls, bool):
            raise ConfigError("MQTT tls must be a boolean")
        if not isinstance(self.allow_insecure_auth, bool):
            raise ConfigError("MQTT allow_insecure_auth must be a boolean")
        if self.password is not None and not self.username:
            raise ConfigError("MQTT password requires a username")
        if (
            (self.username is not None or self.password is not None)
            and not self.tls
            and not self.allow_insecure_auth
        ):
            raise ConfigError(
                "MQTT credentials require TLS; enable tls or explicitly set "
                "allow_insecure_auth for an isolated trusted network"
            )
        if self.profile is not None and self.account is not None and self.profile != self.account:
            raise ConfigError("profile and account selections disagree")
        if self.profile is None:
            object.__setattr__(self, "profile", self.account)

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        dotenv_path: str | Path = ".env",
        **overrides: Any,
    ) -> MqttSettings:
        """Resolve non-secrets from config and credentials from env or keyring."""
        values: dict[str, Any] = {}
        path = Path(config_path) if config_path else default_config_path()
        if path.is_file():
            table = tomllib.loads(path.read_text(encoding="utf-8")).get("mqtt", {})
            for key in (
                "broker",
                "port",
                "topic_prefix",
                "discovery",
                "discovery_prefix",
                "forecast_hours",
                "client_id",
                "tls",
                "allow_insecure_auth",
                "profile",
                "account",
            ):
                if key in table:
                    values[key] = table[key]
        configured_aliases = {str(values[key]) for key in ("profile", "account") if values.get(key)}
        if len(configured_aliases) > 1:
            raise ConfigError("profile and account selections disagree")
        values.pop("account", None)
        if configured_aliases:
            values["profile"] = configured_aliases.pop()

        env = {**load_dotenv(dotenv_path), **os.environ}
        for key, name in (
            ("broker", "TARIFFKIT_MQTT_BROKER"),
            ("port", "TARIFFKIT_MQTT_PORT"),
            ("topic_prefix", "TARIFFKIT_MQTT_TOPIC_PREFIX"),
            ("username", "TARIFFKIT_MQTT_USERNAME"),
            ("password", "TARIFFKIT_MQTT_PASSWORD"),
        ):
            if value := env.get(name):
                values[key] = value
        if value := env.get("TARIFFKIT_MQTT_ALLOW_INSECURE_AUTH"):
            normalized = value.casefold()
            if normalized in {"1", "true", "yes", "on"}:
                values["allow_insecure_auth"] = True
            elif normalized in {"0", "false", "no", "off"}:
                values["allow_insecure_auth"] = False
            else:
                raise ConfigError(
                    "TARIFFKIT_MQTT_ALLOW_INSECURE_AUTH must be a boolean "
                    "(true/false, yes/no, on/off, or 1/0)"
                )
        for name in ("TARIFFKIT_ACCOUNT", "TARIFFKIT_PROFILE"):
            if value := env.get(name):
                values["profile"] = value
                break
        if not values.get("profile"):
            values["profile"] = configured_profile_name(config_path)
        if not values.get("username"):
            values["username"] = get_secret("mqtt.username")
        if not values.get("password"):
            values["password"] = get_secret("mqtt.password")
        profile_overrides = {
            key: overrides.pop(key)
            for key in ("profile", "account", "profile_name")
            if key in overrides and overrides[key] is not None
        }
        selected_profiles = {str(value) for value in profile_overrides.values()}
        if len(selected_profiles) > 1:
            raise ConfigError("profile, account, and profile_name must select the same profile")
        if selected_profiles:
            overrides["profile"] = selected_profiles.pop()
        values.update({key: value for key, value in overrides.items() if value is not None})
        if not values.get("broker"):
            raise ConfigError(
                "MQTT broker not set; configure [mqtt].broker, TARIFFKIT_MQTT_BROKER, or --broker"
            )
        for key in ("port", "forecast_hours"):
            if key in values:
                values[key] = int(values[key])
        return cls(**values)


class MqttPublisher:
    """Publishes at each hour boundary, retained.

    Retaining means a subscriber that connects later immediately receives the
    current price rather than waiting up to an hour for the next tick.
    """

    def __init__(
        self,
        engine: RateEngine | AccountRateEngine,
        settings: MqttSettings,
        client: Any | None = None,
        *,
        profile: AccountProfile | None = None,
        profile_repository: NamedProfileRepository | None = None,
    ) -> None:
        if profile is not None and settings.profile is not None:
            raise ConfigError("choose either an MQTT profile name or an in-memory profile")
        if profile is not None:
            self.engine: RateEngine | AccountRateEngine = AccountRateEngine(profile)
        elif settings.profile is not None:
            repository = profile_repository or NamedProfileRepository()
            self.engine = AccountRateEngine(repository.load(settings.profile))
        else:
            self.engine = engine
        self.settings = settings
        self._stop = threading.Event()
        self._client = self._configure(client if client is not None else self._build_client())

    def _build_client(self) -> Any:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - exercised by packaging
            raise RuntimeError(
                "MQTT support requires the 'mqtt' extra: pip install 'tariffkit[mqtt]'"
            ) from exc

        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.settings.client_id)

    def _configure(self, client: Any) -> Any:
        """Apply auth, TLS, and the last will.

        Kept separate from construction to allow dependency injection of the client
        while still exercising this logic.
        """
        if self.settings.username is not None:
            client.username_pw_set(self.settings.username, self.settings.password)
        if self.settings.tls:
            client.tls_set()
        # Last will, so subscribers see the sensors go unavailable if we die.
        client.will_set(self._topic("status"), OFFLINE, retain=True)
        return client

    def _topic(self, suffix: str) -> str:
        return f"{self.settings.topic_prefix}/{suffix}"

    def _publish(self, suffix: str, payload: Any) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self._client.publish(self._topic(suffix), body, retain=True)

    def connect(self) -> None:
        self._client.connect(self.settings.broker, self.settings.port, keepalive=60)
        self._client.loop_start()
        self._publish("status", ONLINE)
        if self.settings.discovery:
            self.publish_discovery()

    def publish_discovery(self) -> None:
        for topic, payload in discovery_payloads(
            self.engine.describe(),
            topic_prefix=self.settings.topic_prefix,
            discovery_prefix=self.settings.discovery_prefix,
        ):
            self._client.publish(topic, json.dumps(payload), retain=True)
        log.info("published Home Assistant discovery config")

    def _publish_components(self, point: PricePoint) -> None:
        """One retained topic per direction and component group, plus its lines.

        The groups of a direction sum to that direction's price, so a dashboard
        can stack them and get the price back. Each group's attributes topic
        carries the tariff lines rolled into it, which is what makes the
        roll-up auditable from the broker alone.
        """
        for direction, price, groups in (
            ("import", point.import_price, IMPORT_GROUPS),
            ("export", point.export_price, EXPORT_GROUPS),
        ):
            # State from ``grouped()`` rather than re-summing here, so the
            # broker and the custom component cannot drift apart if the
            # roll-up's rounding ever changes; ``split_components`` supplies
            # only the lines behind each band.
            totals = price.grouped()
            lines_by_group = split_components(price.components, groups)
            # Every band repeats its direction's own quality flags. A band is a
            # slice of that price, so it is exactly as trustworthy -- and a
            # subscriber reading one band must not have to know that a second
            # topic is where it would learn the value is delivery-only or past
            # its rate lock. Taken from to_dict() so the two cannot drift, and
            # so each direction carries only the flags it actually has.
            flags = {
                key: value
                for key, value in price.to_dict().items()
                if key in {"complete", "locked", "exact"}
            }
            for group in groups:
                suffix = f"components/{direction}/{group}"
                self._publish(suffix, f"{totals[group]:.5f}")
                self._publish(
                    f"{suffix}/attributes",
                    {"components": dict(lines_by_group[group]), **flags},
                )

    def publish_now(self, moment: datetime | None = None) -> PricePoint:
        now = moment or now_pacific()
        point = self.engine.price_at(now)
        curve = self.engine.forecast(self.settings.forecast_hours, start=point.start)

        self._publish("import_price", f"{point.import_price.total:.5f}")
        self._publish("export_price", f"{point.export_price.total:.5f}")
        self._publish("spread", f"{point.spread:.5f}")
        self._publish("tou_period", str(point.import_price.period))
        self._publish("daily_fixed_charge", f"{self.engine.daily_fixed_charge(point.start):.5f}")
        self._publish_components(point)

        # Component breakdown plus the payloads other energy systems read, so the
        # broker path is as interoperable as the custom component. raw_today and
        # raw_tomorrow are cents (Predbat assumes pence); everything else dollars.
        # Trim against the real moment, not the hour floor: `--once` from cron
        # can land at any minute, and EMHASS's positional lists must start at the
        # 30-minute slot it is actually in.
        emhass = forecast_lists(curve, since=now)
        predbat = predbat_payload(self.engine, now)
        self._publish(
            "import_price/attributes",
            {
                **point.import_price.to_dict(),
                "load_cost_forecast": emhass["load_cost_forecast"],
                "prediction_horizon": emhass["prediction_horizon"],
                **predbat["import"],
            },
        )
        self._publish(
            "export_price/attributes",
            {
                **point.export_price.to_dict(),
                "prod_price_forecast": emhass["prod_price_forecast"],
                "prediction_horizon": emhass["prediction_horizon"],
                **predbat["export"],
            },
        )
        self._publish(
            "spread/attributes",
            {
                "start": point.start.isoformat(),
                "end": point.end.isoformat(),
                # Planners such as EMHASS expect a flat list of hourly entries.
                "forecast": [
                    {
                        "start": p.start.isoformat(),
                        "import": p.import_price.total,
                        "export": p.export_price.total,
                        "spread": round(p.spread, 6),
                    }
                    for p in curve
                ],
            },
        )
        self._publish("forecast", curve.to_dict())
        log.info(
            "published %s import=%.5f export=%.5f",
            point.start.isoformat(),
            point.import_price.total,
            point.export_price.total,
        )
        return point

    def run_forever(self) -> None:
        """Publish immediately, then once at the top of every hour."""
        self.connect()
        self._install_signal_handlers()
        try:
            while not self._stop.is_set():
                self.publish_now()
                delay = (next_hour(now_pacific()) - now_pacific()).total_seconds()
                # Sleep until the boundary rather than polling; wakes early on stop.
                self._stop.wait(max(delay, 1.0))
        finally:
            self.close()

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: FrameType | None) -> None:
            log.info("received signal %s, shutting down", signum)
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            # Raises off the main thread, where signal handling is not ours to
            # install; the caller then drives shutdown via stop().
            with contextlib.suppress(ValueError):
                signal.signal(sig, handle)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        try:
            self._publish("status", OFFLINE)
        finally:
            self._client.loop_stop()
            self._client.disconnect()
