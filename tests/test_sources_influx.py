"""Interval readings from raw counter samples in InfluxDB 3.

The HTTP client is faked here; nothing in this file touches a network. What it
pins is the parts that were wrong or surprising against real data: the
drop-to-zero artefacts the unfiltered counters emit, and the pro-rata spreading
that keeps a sparse sample from shoving energy forward over a TOU boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# The source needs the 'influx' extra; skip rather than fail the whole module
# for a contributor who installed without it.
pytest.importorskip("httpx")

from nem_rates.errors import ConfigError, DataError
from nem_rates.sources import influx
from nem_rates.timeutil import PACIFIC

IMPORT_ID = influx.DEFAULT_IMPORT_ENTITY
EXPORT_ID = influx.DEFAULT_EXPORT_ENTITY


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class FakePost:
    """Answers each query from ``series``, keyed by the entity id in the SQL."""

    def __init__(
        self,
        series: dict[str, list[tuple[datetime, float]]],
        payload: Any = None,
        status_code: int = 200,
    ) -> None:
        self.series = series
        self.payload = payload
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.payload is not None or self.status_code != 200:
            return FakeResponse(self.payload, self.status_code)
        sql = kwargs["json"]["q"]
        rows = next(
            (
                [{"time": t.isoformat(), "value": v} for t, v in s]
                for e, s in self.series.items()
                if f"'{e}'" in sql
            ),
            [],
        )
        return FakeResponse(rows)


@pytest.fixture
def settings() -> influx.InfluxSettings:
    return influx.InfluxSettings(host="influx.example", database="homedb", token="secret")


@pytest.fixture
def patch_post(monkeypatch: pytest.MonkeyPatch) -> Any:
    import httpx

    def install(series: dict[str, list[tuple[datetime, float]]], **kw: Any) -> FakePost:
        fake = FakePost(series, **kw)
        monkeypatch.setattr(httpx, "post", fake)
        return fake

    return install


def at(hour: float, day: int = 1) -> datetime:
    """A Pacific instant on 2026-07-``day``, where hours may be fractional."""
    return datetime(2026, 7, day, tzinfo=PACIFIC) + timedelta(hours=hour)


class TestSettings:
    def test_entities_come_from_the_config_file(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[influxdb]\nhost = "h"\ndatabase = "d"\nimport_entity = "meter_in"\n', encoding="utf-8"
        )
        got = influx.InfluxSettings.load(config, tmp_path / "none", token="t")
        assert got.import_entity == "meter_in"
        assert got.export_entity == EXPORT_ID

    def test_dotenv_supplies_host_and_token(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            'INFLUXDB3_HOST="influx.example"\nINFLUXDB3_DATABASE=homedb\n'
            "INFLUXDB3_AUTH_TOKEN = tok\n",
            encoding="utf-8",
        )
        got = influx.InfluxSettings.load(tmp_path / "none.toml", env)
        assert (got.host, got.database, got.token) == ("influx.example", "homedb", "tok")

    def test_explicit_override_wins_over_everything(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("INFLUXDB3_HOST=a\nINFLUXDB3_DATABASE=b\nINFLUXDB3_AUTH_TOKEN=c\n", "utf-8")
        got = influx.InfluxSettings.load(tmp_path / "none.toml", env, host="override")
        assert got.host == "override"

    def test_missing_credentials_say_what_to_set(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            influx.InfluxSettings.load(tmp_path / "none.toml", tmp_path / "none")
        message = str(caught.value)
        assert "INFLUXDB3_AUTH_TOKEN" in message and "host" in message

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("influx.example", "https://influx.example/api/v3/query_sql"),
            ("https://influx.example/", "https://influx.example/api/v3/query_sql"),
            ("http://box:8181", "http://box:8181/api/v3/query_sql"),
        ],
    )
    def test_query_url(self, host: str, expected: str) -> None:
        got = influx.InfluxSettings(host=host, database="d", token="t")
        assert got.query_url == expected

    def test_a_sensor_prefix_is_stripped(self, tmp_path: Path) -> None:
        got = influx.InfluxSettings.load(
            tmp_path / "none.toml",
            tmp_path / "none",
            host="h",
            database="d",
            token="t",
            import_entity="sensor.meter_in",
        )
        assert got.import_entity == "meter_in"

    @pytest.mark.parametrize("bad", ["meter'; DROP TABLE x--", "meter in", "meter-in"])
    def test_entity_ids_that_could_break_the_sql_are_refused(self, bad: str) -> None:
        # Ids are interpolated into SQL rather than bound, so the constraint is
        # load-bearing, not cosmetic.
        with pytest.raises(ConfigError):
            influx._clean_entity(bad)


class TestMonotonic:
    def test_a_drop_to_zero_is_discarded(self) -> None:
        # The Eagle-100 republishes 0.0 while re-establishing its meter session,
        # about one sample in ten. Differencing across it would invent a huge
        # negative interval and then a compensating spike.
        samples = [(at(0), 100.0), (at(1), 0.0), (at(2), 101.0)]
        assert influx.monotonic(samples) == [(at(0), 100.0), (at(2), 101.0)]

    def test_a_backwards_reading_is_discarded(self) -> None:
        samples = [(at(0), 100.0), (at(1), 99.0), (at(2), 101.0)]
        assert [v for _, v in influx.monotonic(samples)] == [100.0, 101.0]

    def test_a_flat_counter_survives(self) -> None:
        # Equal is not backwards: a counter that did not move is real data.
        samples = [(at(0), 100.0), (at(1), 100.0)]
        assert influx.monotonic(samples) == samples


class TestReadCounters:
    def test_totals_are_the_endpoint_difference(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        patch_post(
            {
                IMPORT_ID: [(at(-1), 10.0), (at(0), 12.0), (at(1), 15.0), (at(2), 20.0)],
                EXPORT_ID: [(at(-1), 5.0), (at(2), 9.0)],
            }
        )
        got = influx.read_counters(settings, at(0), at(2))
        assert sum(r.imported for r in got) == pytest.approx(8.0)
        # 4.0 accrued over 3h from -1 to 2; only the 2h inside the window count.
        assert sum(r.exported for r in got) == pytest.approx(4.0 * 2 / 3)

    def test_an_advance_is_spread_over_the_span_it_covers(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        # One sample at 02:00 reporting 4 kWh since 00:00 is not 4 kWh at 02:00.
        # Crediting it to the later hour is what shoved export into peak.
        patch_post({IMPORT_ID: [(at(0), 10.0), (at(2), 14.0)], EXPORT_ID: []})
        got = influx.read_counters(settings, at(0), at(2))
        assert [r.imported for r in got] == pytest.approx([2.0, 2.0])

    def test_a_sample_straddling_a_boundary_splits_by_time(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        # 00:45 -> 01:15 is a quarter-hour either side, so the 6 kWh halves.
        patch_post({IMPORT_ID: [(at(0.75), 10.0), (at(1.25), 16.0)], EXPORT_ID: []})
        got = influx.read_counters(settings, at(0), at(2))
        assert [r.imported for r in got] == pytest.approx([3.0, 3.0])

    def test_a_baseline_before_the_window_is_clipped_not_dumped(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        # Without clipping, a baseline reaching back days would put its whole
        # span's energy in the first interval.
        patch_post({IMPORT_ID: [(at(-23), 10.0), (at(1), 34.0)], EXPORT_ID: []})
        got = influx.read_counters(settings, at(0), at(1))
        assert got[0].imported == pytest.approx(1.0)

    def test_readings_come_back_in_order_and_at_the_resolution(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        patch_post({IMPORT_ID: [(at(0), 10.0), (at(3), 13.0)], EXPORT_ID: []})
        got = influx.read_counters(settings, at(0), at(3), resolution=timedelta(minutes=30))
        assert len(got) == 6
        assert [r.start for r in got] == sorted(r.start for r in got)
        assert all(r.duration == timedelta(minutes=30) for r in got)

    def test_intervals_step_in_absolute_time_across_a_dst_fall_back(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        # 2026-11-01 is 25 hours long in Pacific; assuming 24 would drop one.
        start = datetime(2026, 11, 1, tzinfo=PACIFIC)
        end = datetime(2026, 11, 2, tzinfo=PACIFIC)
        patch_post({IMPORT_ID: [(start, 10.0), (end, 35.0)], EXPORT_ID: []})
        got = influx.read_counters(settings, start, end)
        assert len(got) == 25
        assert len({r.start.astimezone(UTC) for r in got}) == 25

    def test_a_window_query_asks_for_a_baseline_before_it(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        fake = patch_post({IMPORT_ID: [(at(0), 10.0), (at(1), 11.0)], EXPORT_ID: []})
        influx.read_counters(settings, at(0), at(1))
        lower = (at(0) - influx.BASELINE_LOOKBACK).astimezone(UTC)
        assert f"{lower:%Y-%m-%dT%H:%M:%SZ}" in fake.calls[0]["json"]["q"]

    def test_the_token_travels_as_a_bearer_header(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        fake = patch_post({IMPORT_ID: [(at(0), 10.0), (at(1), 11.0)], EXPORT_ID: []})
        influx.read_counters(settings, at(0), at(1))
        assert fake.calls[0]["headers"]["Authorization"] == "Bearer secret"
        assert fake.calls[0]["json"]["db"] == "homedb"

    def test_no_samples_names_the_entities(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        patch_post({})
        with pytest.raises(DataError) as caught:
            influx.read_counters(settings, at(0), at(1))
        assert IMPORT_ID in str(caught.value) and EXPORT_ID in str(caught.value)

    def test_a_refused_query_surfaces_the_status(
        self, settings: influx.InfluxSettings, patch_post: Any
    ) -> None:
        patch_post({}, payload={"error": "no such table"}, status_code=404)
        with pytest.raises(DataError) as caught:
            influx.read_counters(settings, at(0), at(1))
        assert "404" in str(caught.value)

    @pytest.mark.parametrize("which", ["start", "end"])
    def test_naive_datetimes_are_rejected(
        self, settings: influx.InfluxSettings, which: str
    ) -> None:
        window = {"start": at(0), "end": at(1)}
        window[which] = window[which].replace(tzinfo=None)
        with pytest.raises(ConfigError, match="timezone-aware"):
            influx.read_counters(settings, **window)

    def test_backwards_window_is_rejected(self, settings: influx.InfluxSettings) -> None:
        with pytest.raises(ConfigError, match="not after"):
            influx.read_counters(settings, at(2), at(1))

    def test_nonpositive_resolution_is_rejected(self, settings: influx.InfluxSettings) -> None:
        with pytest.raises(ConfigError, match="positive"):
            influx.read_counters(settings, at(0), at(1), resolution=timedelta(0))
