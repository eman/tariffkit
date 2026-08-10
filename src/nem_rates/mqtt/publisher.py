"""Publish current and forecast prices to an MQTT broker."""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from types import FrameType
from typing import Any

from ..engine import RateEngine
from ..interop import forecast_lists, predbat_payload
from ..models import PricePoint
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
    topic_prefix: str = "nem_rates"
    discovery: bool = True
    discovery_prefix: str = "homeassistant"
    forecast_hours: int = 48
    client_id: str = "nem-rates"
    tls: bool = False


class MqttPublisher:
    """Publishes at each hour boundary, retained.

    Retaining means a subscriber that connects later immediately receives the
    current price rather than waiting up to an hour for the next tick.
    """

    def __init__(
        self, engine: RateEngine, settings: MqttSettings, client: Any | None = None
    ) -> None:
        self.engine = engine
        self.settings = settings
        self._stop = threading.Event()
        self._client = self._configure(client if client is not None else self._build_client())

    def _build_client(self) -> Any:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - exercised by packaging
            raise RuntimeError(
                "MQTT support requires the 'mqtt' extra: pip install 'nem-rates[mqtt]'"
            ) from exc

        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.settings.client_id)

    def _configure(self, client: Any) -> Any:
        """Apply auth, TLS, and the last will.

        Kept separate from construction so tests can inject a fake client and
        still exercise this, rather than stubbing it out along with the client.
        """
        if self.settings.username:
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

    def publish_now(self, moment: datetime | None = None) -> PricePoint:
        now = moment or now_pacific()
        point = self.engine.price_at(now)
        curve = self.engine.forecast(self.settings.forecast_hours, start=point.start)

        self._publish("import_price", f"{point.import_price.total:.5f}")
        self._publish("export_price", f"{point.export_price.total:.5f}")
        self._publish("spread", f"{point.spread:.5f}")
        self._publish("tou_period", str(point.import_price.period))

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
