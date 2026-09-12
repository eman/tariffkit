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

from conftest import CaptureLogs

# The source needs the 'ha' extra; skip rather than fail the whole module for a
# contributor who installed without it.
pytest.importorskip("websockets")

from tariffkit.account import MeterSource
from tariffkit.billing import BillingPeriod, check_coverage
from tariffkit.errors import ConfigError, DataError
from tariffkit.sources import homeassistant as ha
from tariffkit.timeutil import PACIFIC

IMPORT_ID = "sensor.grid_import_total"
EXPORT_ID = "sensor.grid_export_total"


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
    # The entities are no longer defaulted, so a fixture that reads meters has
    # to name them, the same as a real configuration does.
    return ha.HaSettings(
        host="https://ha.example:8123",
        token="tok",
        import_entity=IMPORT_ID,
        export_entity=EXPORT_ID,
    )


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
        # Naming one and not the other leaves the other unset rather than
        # substituting a guess.
        assert s.export_entity is None

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

    def test_profile_mapping_wins_over_environment_but_cli_wins_over_profile(
        self, tmp_path: Path
    ) -> None:
        env = tmp_path / ".env"
        env.write_text(
            'HA_HOST = "http://env"\nHA_TOKEN = "tok"\n'
            'TARIFFKIT_HA_IMPORT_ENTITY = "sensor.env_in"\n'
            'TARIFFKIT_HA_EXPORT_ENTITY = "sensor.env_out"\n'
        )
        profile = MeterSource("sensor.profile_in", "sensor.profile_out")
        settings = ha.HaSettings.load(
            dotenv_path=env,
            profile_source=profile,
            import_entity="sensor.cli_in",
        )
        assert settings.import_entity == "sensor.cli_in"
        assert settings.export_entity == "sensor.profile_out"

        settings = ha.HaSettings.load(dotenv_path=env, profile_source=profile)
        assert (settings.import_entity, settings.export_entity) == (
            "sensor.profile_in",
            "sensor.profile_out",
        )

    def test_empty_override_does_not_clear_a_real_value(self, tmp_path: Path) -> None:
        """argparse hands through None for a flag nobody passed."""
        env = tmp_path / ".env"
        env.write_text('HA_HOST = "http://env"\nHA_TOKEN = "tok"\n')
        env2 = tmp_path / "cfg.toml"
        env2.write_text(
            '[home_assistant]\nimport_entity = "sensor.from_config"\n', encoding="utf-8"
        )
        s = ha.HaSettings.load(config_path=env2, dotenv_path=env, import_entity=None)
        # The override is absent, not empty: dropping the `if v` filter would
        # write None over the configured value and lose it.
        assert s.import_entity == "sensor.from_config"

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

    def test_a_backwards_counter_is_refused_not_negated(self, settings: ha.HaSettings) -> None:
        """It is not energy flowing the other way, and it is not zero either.

        This asserted `imported == 0.0` against a series whose export half was
        absent, and passed for the wrong reason: the refused value *reads* as
        0.0 because a float cannot say "unknown", and the absent export was
        being taken for a measured zero. With a real export row the refusal is
        visible where it belongs.
        """
        step = timedelta(minutes=5)
        start = datetime(2026, 7, 1, 12, tzinfo=PACIFIC)
        series = {IMPORT_ID: [point(start, -3.0, step)], EXPORT_ID: [point(start, 2.0, step)]}

        reading = next(iter(ha._readings_from(series, settings, step).values()))

        assert reading.imported == 0.0
        assert reading.exported == 2.0
        assert reading.unmetered == frozenset({"imported"})

    def test_an_absent_row_is_unknown_rather_than_a_measured_zero(
        self, settings: ha.HaSettings
    ) -> None:
        """The recorder compiles an hour for any entity that has a state.

        A flat counter still yields a change of zero, so no row at all means the
        entity had no state -- not that nothing crossed the meter. An interval
        with neither direction known is dropped, and coverage reports the hole.
        """
        step = timedelta(minutes=5)
        start = datetime(2026, 7, 1, 12, tzinfo=PACIFIC)

        one_side = ha._readings_from(
            {IMPORT_ID: [point(start, 1.5, step)], EXPORT_ID: []}, settings, step
        )
        (reading,) = one_side.values()
        assert reading.imported == 1.5
        assert reading.unmetered == frozenset({"exported"})

        neither = ha._readings_from(
            {IMPORT_ID: [point(start, -3.0, step)], EXPORT_ID: []}, settings, step
        )
        assert neither == {}

    def test_a_running_sum_restart_is_discarded(
        self, settings: ha.HaSettings, captured_logs: CaptureLogs
    ) -> None:
        """The failure this filter exists for, taken from a real instance.

        When recording is interrupted the statistics ``sum`` restarts, and the
        first point of the new epoch reports the whole accumulated total as its
        ``change`` -- 543.663 kWh inside one five-minute slot, about 6,500 kW.
        Discarded rather than clamped: it is not a large reading, it is not a
        reading, and the hole it leaves is something coverage checking can report.

        Uses ``captured_logs`` rather than ``caplog``: the Home Assistant test
        plugin overrides that fixture by requesting it, which pytest 9 refuses
        as a recursive dependency, and this test spent that whole window
        uncollectable while looking like a passing suite.
        """
        logs = captured_logs(ha.log)
        step = timedelta(minutes=5)
        start = datetime(2026, 8, 1, 4, 15, tzinfo=PACIFIC)
        series = {
            IMPORT_ID: [point(start, 543.663, step), point(start + step, 0.02, step)],
            EXPORT_ID: [point(start, 796.079, step), point(start + step, 0.0, step)],
        }
        got = ha._readings_from(series, settings, step)
        assert len(got) == 1
        assert next(iter(got.values())).imported == 0.02
        assert "could not be differenced either" in logs.text

    def test_one_meters_restart_does_not_discard_the_other_meters_energy(
        self, settings: ha.HaSettings
    ) -> None:
        """Import and export restart their sums independently.

        Refusing the whole interval when either did let a bad series destroy the
        good one beside it. Taken from a real account whose unfiltered
        export counter resets its meter session 5.5 times a day: the export
        series' 56 bad hours took 21.4 kWh of good import with them, and a
        74.5 kWh cycle was billed as 53.1.
        """
        step = timedelta(hours=1)
        start = datetime(2026, 8, 29, 16, tzinfo=PACIFIC)
        series = {
            IMPORT_ID: [point(start, 1.872, step)],
            EXPORT_ID: [point(start, 1463.578, step)],
        }
        got = ha._readings_from(series, settings, step)
        assert len(got) == 1, "the interval survives on its good half"
        reading = next(iter(got.values()))
        assert reading.imported == 1.872, "the import meter said nothing wrong"
        assert reading.exported == 0.0, "the export half is refused, not clamped"
        # And the refusal is on the record, so the zero above cannot be mistaken
        # for a measured hour of no export.
        assert reading.unmetered == frozenset({"exported"})
        problems = list(check_coverage([reading], BillingPeriod(start.date(), start.date())))
        assert any("no exported reading" in p for p in problems)
        assert not any("no imported reading" in p for p in problems)

    def test_both_meters_restarting_still_drops_the_interval(self, settings: ha.HaSettings) -> None:
        """Nothing is left to keep, so the hole is worth more than the row.

        The counterpart to the test above: judging directions separately must
        not turn an interval with no usable half into a row of two zeros, which
        coverage checking would accept as a measured hour of no energy.
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

    def test_a_spoiled_interval_is_repaired_from_the_counter(self, settings: ha.HaSettings) -> None:
        """The same repair the integration does, now in one place.

        A `total_increasing` sensor reading 0.0 is taken for a counter reset, so
        the recorder reports the whole counter as the next interval's `change`.
        The counter is intact in `state`, and differencing it recovers the
        interval. This reader used to drop it while the integration's reader
        repaired it -- two readers of the same statistics, disagreeing.
        """
        step = timedelta(minutes=5)
        start = datetime(2026, 8, 1, 4, 15, tzinfo=PACIFIC)
        series = {
            IMPORT_ID: [
                {**point(start - step, 0.02, step), "state": 1460.58},
                {**point(start, 1460.60, step), "state": 1461.0},
            ],
            EXPORT_ID: [],
        }
        got = ha._readings_from(series, settings, step)
        assert len(got) == 2, "the spoiled interval survives"
        assert list(got.values())[1].imported == pytest.approx(0.42)

    def test_a_partly_covered_hour_falls_back_to_the_hourly_row(
        self, settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An hour the fine series only half covers must not lose the other half.

        Dropping the hourly row for any hour with *some* fine data left the
        uncovered part in neither series. On a real cycle the five-minute
        statistics resumed at 04:20 after a restart, deleting the 04:00 hourly
        row and leaving 04:00-04:20 nowhere -- reported as a gap, and short by
        whatever crossed the meter in those twenty minutes.
        """
        hour = datetime(2026, 8, 30, 4, tzinfo=PACIFIC)
        step = timedelta(minutes=5)
        fine = {
            IMPORT_ID: [point(hour + timedelta(minutes=m), 0.01, step) for m in range(20, 60, 5)],
            EXPORT_ID: [],
        }
        whole = {IMPORT_ID: [point(hour, 0.50, timedelta(hours=1))], EXPORT_ID: []}
        patch_socket(monkeypatch, {"5minute": fine, "hour": whole})
        got = ha.read_statistics(settings, hour, hour + timedelta(hours=1), resolution="auto")
        assert len(got) == 1, "the hour, not the eight slots that half-cover it"
        assert got[0].duration == timedelta(hours=1)
        assert got[0].imported == pytest.approx(0.50)

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


class TestSyncWrapper:
    def test_refuses_to_run_inside_an_event_loop(self, settings: ha.HaSettings) -> None:
        """asyncio.run would raise a bare RuntimeError saying nothing useful."""
        import asyncio

        async def inner() -> None:
            with pytest.raises(ConfigError, match="read_statistics_async"):
                ha.read_statistics(
                    settings,
                    datetime(2026, 7, 1, tzinfo=PACIFIC),
                    datetime(2026, 7, 2, tzinfo=PACIFIC),
                )

        asyncio.run(inner())

    @pytest.mark.parametrize("which", ["start", "end"])
    def test_naive_datetimes_are_rejected(self, settings: ha.HaSettings, which: str) -> None:
        """They do not raise on their own -- astimezone silently assumes the
        machine's timezone, so the window would be quietly wrong."""
        import asyncio

        window = {
            "start": datetime(2026, 7, 1, tzinfo=PACIFIC),
            "end": datetime(2026, 7, 2, tzinfo=PACIFIC),
        }
        window[which] = window[which].replace(tzinfo=None)
        with pytest.raises(ConfigError, match="timezone-aware"):
            asyncio.run(ha.read_statistics_async(settings, window["start"], window["end"]))


def test_interleaved_messages_do_not_confuse_the_response(
    settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant may push other frames between request and reply."""
    hour = timedelta(hours=1)
    h0 = datetime(2026, 7, 1, tzinfo=PACIFIC)
    socket = patch_socket(
        monkeypatch,
        {"hour": {IMPORT_ID: [point(h0, 3.0, hour)], EXPORT_ID: []}, "5minute": {}},
    )
    original = socket.send

    async def send_with_noise(raw: str) -> None:
        await original(raw)
        if json.loads(raw).get("type") == "recorder/statistics_during_period":
            # An unrelated event arrives before the answer we asked for.
            socket.outbox.insert(-1, json.dumps({"id": 999, "type": "event"}))

    monkeypatch.setattr(socket, "send", send_with_noise)
    got = ha.read_statistics(settings, h0, h0 + timedelta(hours=2), resolution="hour")
    assert [r.imported for r in got] == [3.0]


def test_describe_resolution_names_a_mixed_run() -> None:
    """One run can mix resolutions, so the caller can say so rather than imply
    uniformity."""
    from tariffkit.billing import IntervalReading

    start = datetime(2026, 7, 1, tzinfo=PACIFIC)
    readings = [
        IntervalReading(start, imported=1.0, duration=timedelta(minutes=5)),
        IntervalReading(start, imported=1.0, duration=timedelta(minutes=5)),
        IntervalReading(start, imported=1.0, duration=timedelta(hours=1)),
    ]
    assert ha.describe_resolution(readings) == "2 x 5minute, 1 x hour"


def test_a_partial_hour_keeps_its_fine_rows_when_nothing_replaces_them(
    settings: ha.HaSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window beginning mid-hour has no hourly row, by construction.

    Surrendering a partial hour's five-minute rows is the right trade only when
    there is an hourly row to surrender them *to*. Doing it unconditionally
    threw away every reading in such a window: an explicit five-minute request
    for 04:20-05:00 had eight rows per entity on a real instance and raised
    "no statistics for ... between ...".
    """
    step = timedelta(minutes=5)
    start = datetime(2026, 7, 1, 4, 20, tzinfo=PACIFIC)
    end = start + step * 8
    patch_socket(
        monkeypatch,
        {
            "5minute": {
                IMPORT_ID: [point(start + step * n, 0.1, step) for n in range(8)],
                EXPORT_ID: [point(start + step * n, 0.0, step) for n in range(8)],
            },
            "hour": {},
        },
    )

    readings = ha.read_statistics(settings, start, end, resolution="5minute")

    assert len(readings) == 8
    assert sum(r.imported for r in readings) == pytest.approx(0.8)


class TestOptionalEntities:
    """Naming the grid counters is optional; reading them without is an error."""

    def test_settings_load_without_any_entity(self, tmp_path: Path) -> None:
        """Rate pricing needs no meter, so loading must not demand one.

        There used to be a default pair here, named for one site's hardware, so
        "not configured" was indistinguishable from "configured as somebody
        else's sensors" -- and the request went out to Home Assistant either way.
        """
        env = tmp_path / ".env"
        env.write_text('HA_HOST = "http://env"\nHA_TOKEN = "tok"\n')
        s = ha.HaSettings.load(config_path=tmp_path / "none.toml", dotenv_path=env)
        assert (s.import_entity, s.export_entity) == (None, None)

    def test_reading_without_entities_says_how_to_set_them(self, tmp_path: Path) -> None:
        s = ha.HaSettings(host="https://ha.example", token="tok")
        with pytest.raises(ConfigError) as err:
            ha.read_statistics(
                s,
                datetime(2026, 7, 1, tzinfo=PACIFIC),
                datetime(2026, 7, 2, tzinfo=PACIFIC),
            )
        message = str(err.value)
        assert "import_entity and export_entity not set" in message
        assert "tariffkit account source set ha" in message
        # And it says what still works, so the answer is not "configure a meter
        # or get nothing".
        assert "needs neither" in message


class TestUpgradingFrom081:
    """What 0.8.1 read by default is a fact about the user's past, not a guess."""

    def test_the_error_names_what_the_previous_version_used(self) -> None:
        """Anyone who never configured these was relying on those two names.

        Telling them to pass `--grid-import-entity ...` left a reader who never
        knew there were defaults with nothing to type.
        """
        settings = ha.HaSettings(host="https://ha.example", token="tok")
        with pytest.raises(ConfigError) as err:
            settings.require_entities()
        message = str(err.value)
        assert "sensor.eagle_100_energy_delivered" in message
        assert "sensor.eagle_100_energy_received" in message
        assert "0.8.1 or earlier" in message
        # And a command that can be pasted, not a placeholder.
        assert "tariffkit account source set ha" in message
        assert "--grid-import-entity ..." not in message
