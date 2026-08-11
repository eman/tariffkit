"""Interval readings from Home Assistant statistics.

The WebSocket is faked here; nothing in this file touches a network. What it
pins is the parts that were wrong or surprising against a real instance: how a
running-sum restart looks, which resolution wins where both exist, and how
settings resolve across a config file, a .env and the environment.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nem_rates.errors import ConfigError, DataError
from nem_rates.sources import homeassistant as ha
from nem_rates.timeutil import PACIFIC

IMPORT_ID = ha.DEFAULT_IMPORT_ENTITY
EXPORT_ID = ha.DEFAULT_EXPORT_ENTITY


def epoch_ms(moment: datetime) -> int:
    return int(moment.astimezone(UTC).timestamp() * 1000)


def point(start: datetime, change: float, step: timedelta) -> dict[str, Any]:
    return {"start": epoch_ms(start), "end": epoch_ms(start + step), "change": change}


class FakeSocket:
    """Scripts the auth handshake, then answers each request from ``series``."""

    def __init__(self, series: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self.series = series
        self.outbox: list[str] = ["auth_required", "auth_ok"]
        self.requested: list[str] = []

    async def recv(self) -> str:
        head = self.outbox.pop(0)
        if head in ("auth_required", "auth_ok"):
            return json.dumps({"type": head})
        return head

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        if message.get("type") != "recorder/statistics_during_period":
            return
        period = message["period"]
        self.requested.append(period)
        self.outbox.append(
            json.dumps(
                {"id": message["id"], "success": True, "result": self.series.get(period, {})}
            )
        )

    async def __aenter__(self) -> FakeSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def settings() -> ha.HaSettings:
    return ha.HaSettings(host="https://ha.example:8123", token="tok")


def patch_socket(monkeypatch: pytest.MonkeyPatch, series: dict[str, Any]) -> FakeSocket:
    socket = FakeSocket(series)
    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: socket)
    return socket


class TestDotenv:
    def test_tolerates_spaces_and_quotes(self, tmp_path: Path) -> None:
        """How these files are actually written by hand."""
        env = tmp_path / ".env"
        env.write_text("# a comment\nHA_TOKEN = \"abc\"\nHA_HOST='http://x'\n\nJUNK\n")
        assert ha.load_dotenv(env) == {"HA_TOKEN": "abc", "HA_HOST": "http://x"}

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """The token may equally come from the environment."""
        assert ha.load_dotenv(tmp_path / "absent") == {}


class TestSettings:
    def test_entities_come_from_the_config_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[home_assistant]\nhost = "http://cfg:8123"\nimport_entity = "sensor.in"\n')
        env = tmp_path / ".env"
        env.write_text('HA_TOKEN = "tok"\n')
        s = ha.HaSettings.load(config_path=cfg, dotenv_path=env)
        assert (s.host, s.import_entity, s.token) == ("http://cfg:8123", "sensor.in", "tok")
        assert s.export_entity == ha.DEFAULT_EXPORT_ENTITY

    def test_dotenv_host_wins_over_the_config_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[home_assistant]\nhost = "http://cfg:8123"\n')
        env = tmp_path / ".env"
        env.write_text('HA_HOST = "http://env:8123"\nHA_TOKEN = "tok"\n')
        assert ha.HaSettings.load(config_path=cfg, dotenv_path=env).host == "http://env:8123"

    def test_explicit_override_wins_over_everything(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('HA_HOST = "http://env"\nHA_TOKEN = "tok"\n')
        s = ha.HaSettings.load(dotenv_path=env, import_entity="sensor.override")
        assert s.import_entity == "sensor.override"

    def test_empty_override_does_not_clear_a_real_value(self, tmp_path: Path) -> None:
        """argparse hands through None for a flag nobody passed."""
        env = tmp_path / ".env"
        env.write_text('HA_HOST = "http://env"\nHA_TOKEN = "tok"\n')
        s = ha.HaSettings.load(dotenv_path=env, import_entity=None)
        assert s.import_entity == ha.DEFAULT_IMPORT_ENTITY

    def test_missing_token_says_what_to_set(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('HA_HOST = "http://env"\n')
        with pytest.raises(ConfigError, match="HA_TOKEN"):
            ha.HaSettings.load(config_path=tmp_path / "none.toml", dotenv_path=env)

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("https://h:8123", "wss://h:8123/api/websocket"),
            ("http://h:8123", "ws://h:8123/api/websocket"),
            ("https://h:8123/", "wss://h:8123/api/websocket"),
        ],
    )
    def test_websocket_url(self, host: str, expected: str) -> None:
        assert ha.HaSettings(host=host, token="t").websocket_url == expected


class TestReadings:
    def test_change_is_used_directly(self, settings: ha.HaSettings) -> None:
        """``change`` is already the energy within its own period.

        Differencing ``sum`` between points would need an off-by-one correction
        and would throw away the first point.
        """
        step = timedelta(minutes=5)
        start = datetime(2026, 7, 1, 12, tzinfo=PACIFIC)
        series = {
            IMPORT_ID: [point(start, 0.4, step), point(start + step, 0.6, step)],
            EXPORT_ID: [point(start, 0.0, step), point(start + step, 1.5, step)],
        }
        got = ha._readings_from(series, settings, step)
        assert [r.imported for r in got.values()] == [0.4, 0.6]
        assert [r.exported for r in got.values()] == [0.0, 1.5]

    def test_both_directions_in_one_slot_are_kept(self, settings: ha.HaSettings) -> None:
        """Import and export are metered separately, so this is real, not gross data."""
        step = timedelta(hours=1)
        start = datetime(2026, 7, 1, 12, tzinfo=PACIFIC)
        series = {IMPORT_ID: [point(start, 0.5, step)], EXPORT_ID: [point(start, 2.0, step)]}
        reading = next(iter(ha._readings_from(series, settings, step).values()))
        assert (reading.imported, reading.exported) == (0.5, 2.0)

    def test_a_backwards_counter_is_clamped_not_negated(self, settings: ha.HaSettings) -> None:
        step = timedelta(minutes=5)
        start = datetime(2026, 7, 1, 12, tzinfo=PACIFIC)
        series = {IMPORT_ID: [point(start, -3.0, step)], EXPORT_ID: []}
        assert next(iter(ha._readings_from(series, settings, step).values())).imported == 0.0

    def test_a_running_sum_restart_is_discarded(
        self, settings: ha.HaSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure this filter exists for, taken from a real instance.

        When recording is interrupted the statistics ``sum`` restarts, and the
        first point of the new epoch reports the whole accumulated total as its
        ``change`` -- 543.663 kWh inside one five-minute slot, about 6,500 kW.
        Discarded rather than clamped: it is not a large reading, it is not a
        reading, and the hole it leaves is something coverage checking can report.
        """
        step = timedelta(minutes=5)
        start = datetime(2026, 8, 1, 4, 15, tzinfo=PACIFIC)
        series = {
            IMPORT_ID: [point(start, 543.663, step), point(start + step, 0.02, step)],
            EXPORT_ID: [point(start, 796.079, step), point(start + step, 0.0, step)],
        }
        got = ha._readings_from(series, settings, step)
        assert len(got) == 1
        assert next(iter(got.values())).imported == 0.02
        assert "running sum restarted" in caplog.text

    def test_a_plausible_large_interval_survives(self, settings: ha.HaSettings) -> None:
        """The ceiling only ever catches the impossible, not a heavy hour."""
        step = timedelta(hours=1)
        start = datetime(2026, 7, 1, 18, tzinfo=PACIFIC)
        series = {IMPORT_ID: [point(start, 40.0, step)], EXPORT_ID: []}
        assert next(iter(ha._readings_from(series, settings, step).values())).imported == 40.0


class TestReadStatistics:
    def series(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Two hours of hourly, with five-minute covering only the second."""
        hour, five = timedelta(hours=1), timedelta(minutes=5)
        h0 = datetime(2026, 7, 1, 0, tzinfo=PACIFIC)
        h1 = h0 + hour
        return {
            "hour": {
                IMPORT_ID: [point(h0, 1.0, hour), point(h1, 2.0, hour)],
                EXPORT_ID: [point(h0, 0.0, hour), point(h1, 0.0, hour)],
            },
            "5minute": {
                IMPORT_ID: [point(h1 + five * i, 0.25, five) for i in range(12)],
                EXPORT_ID: [point(h1 + five * i, 0.0, five) for i in range(12)],
            },
        }

    def read(self, settings: ha.HaSettings, **kw: Any) -> list[Any]:
        return ha.read_statistics(
            settings,
            datetime(2026, 7, 1, tzinfo=PACIFIC),
            datetime(2026, 7, 1, 2, tzinfo=PACIFIC),
            **kw,
        )

    def test_finer_resolution_replaces_the_hour_it_covers(
        self, settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_socket(monkeypatch, self.series())
        got = self.read(settings)
        # Hour 0 stays hourly; hour 1 is replaced by its twelve five-minute slots.
        assert [r.duration for r in got].count(timedelta(hours=1)) == 1
        assert [r.duration for r in got].count(timedelta(minutes=5)) == 12
        # Energy is counted once, not at both resolutions.
        assert sum(r.imported for r in got) == pytest.approx(1.0 + 12 * 0.25)

    def test_auto_requests_both_resolutions(
        self, settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        socket = patch_socket(monkeypatch, self.series())
        self.read(settings)
        assert sorted(socket.requested) == ["5minute", "hour"]

    def test_an_explicit_resolution_asks_for_only_that(
        self, settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        socket = patch_socket(monkeypatch, self.series())
        got = self.read(settings, resolution="hour")
        assert socket.requested == ["hour"]
        assert {r.duration for r in got} == {timedelta(hours=1)}

    def test_readings_come_back_in_order(
        self, settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_socket(monkeypatch, self.series())
        starts = [r.start for r in self.read(settings)]
        assert starts == sorted(starts)

    def test_no_statistics_names_the_entities(
        self, settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_socket(monkeypatch, {"hour": {}, "5minute": {}})
        with pytest.raises(DataError, match=IMPORT_ID):
            self.read(settings)

    def test_backwards_window_is_rejected(self, settings: ha.HaSettings) -> None:
        with pytest.raises(ConfigError, match="not after"):
            ha.read_statistics(
                settings,
                datetime(2026, 7, 2, tzinfo=PACIFIC),
                datetime(2026, 7, 1, tzinfo=PACIFIC),
            )

    def test_unknown_resolution_is_rejected(self, settings: ha.HaSettings) -> None:
        with pytest.raises(ConfigError, match="unknown resolution"):
            self.read(settings, resolution="daily")


def test_describe_resolution_names_a_mixed_run() -> None:
    """One run can mix resolutions, so the caller can say so rather than imply
    uniformity."""
    from nem_rates.billing import IntervalReading

    start = datetime(2026, 7, 1, tzinfo=PACIFIC)
    readings = [
        IntervalReading(start, imported=1.0, duration=timedelta(minutes=5)),
        IntervalReading(start, imported=1.0, duration=timedelta(minutes=5)),
        IntervalReading(start, imported=1.0, duration=timedelta(hours=1)),
    ]
    assert ha.describe_resolution(readings) == "2 x 5minute, 1 x hour"
