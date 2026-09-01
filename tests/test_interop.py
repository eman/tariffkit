"""Adapters for the Home Assistant Energy dashboard, EMHASS, and Predbat.

These assert on payload structure without importing ``homeassistant``, the same
way ``TestDiscovery`` covers the MQTT discovery payloads.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from tariffkit import PriceCurve, RateEngine
from tariffkit.interop import (
    forecast_lists,
    forecast_payload,
    local_day_window,
    predbat_payload,
    raw_attributes,
    resample,
)
from tariffkit.interop.predbat import CENTS_PER_DOLLAR
from tariffkit.timeutil import PACIFIC


def pt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=PACIFIC)


@pytest.fixture
def engine() -> RateEngine:
    return RateEngine()


@pytest.fixture
def curve(engine: RateEngine) -> PriceCurve:
    """Two ordinary summer days, no DST transition."""
    return engine.forecast(hours=48, start=pt(2026, 7, 15))


class TestResample:
    @pytest.mark.parametrize(("minutes", "expected"), [(60, 24), (30, 48), (15, 96), (5, 288)])
    def test_slot_count(self, engine: RateEngine, minutes: int, expected: int) -> None:
        day = engine.forecast(hours=24, start=pt(2026, 7, 15))
        assert len(resample(day, minutes)) == expected

    def test_sixty_minutes_is_a_passthrough(self, curve: PriceCurve) -> None:
        assert resample(curve, 60) == curve.points

    @pytest.mark.parametrize("minutes", [0, -30, 7, 45, 90])
    def test_rejects_non_divisors_of_an_hour(self, curve: PriceCurve, minutes: int) -> None:
        with pytest.raises(ValueError, match="divisor of 60"):
            resample(curve, minutes)

    def test_slots_are_contiguous_and_disjoint(self, curve: PriceCurve) -> None:
        slots = resample(curve, 30)
        for earlier, later in pairwise(slots):
            assert earlier.end.astimezone(UTC) == later.start.astimezone(UTC)

    def test_every_slot_is_exactly_the_requested_length(self, curve: PriceCurve) -> None:
        for slot in resample(curve, 30):
            span = slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
            assert span == timedelta(minutes=30)

    def test_price_is_repeated_across_the_hour_not_interpolated(self, curve: PriceCurve) -> None:
        first, second = resample(curve, 30)[:2]
        assert first.import_price.total == second.import_price.total
        assert first.export_price.total == second.export_price.total

    def test_slots_align_to_the_half_hour(self, curve: PriceCurve) -> None:
        """Predbat requires entries landing on :00 and :30."""
        assert {s.start.minute for s in resample(curve, 30)} == {0, 30}


class TestResampleAcrossDst:
    """The 23- and 25-hour days are where a naive split goes wrong."""

    @pytest.mark.parametrize(
        ("start", "hours", "expected"),
        [(pt(2026, 11, 1), 25, 50), (pt(2027, 3, 14), 23, 46)],
        ids=["fall-back", "spring-forward"],
    )
    def test_slot_count_follows_the_real_day_length(
        self, engine: RateEngine, start: datetime, hours: int, expected: int
    ) -> None:
        assert len(resample(engine.forecast(hours=hours, start=start), 30)) == expected

    def test_repeated_hour_yields_distinct_instants(self, engine: RateEngine) -> None:
        slots = resample(engine.forecast(hours=25, start=pt(2026, 11, 1)), 30)
        instants = [s.start.astimezone(UTC) for s in slots]
        assert len(set(instants)) == len(instants)

    def test_both_one_am_hours_survive_the_split(self, engine: RateEngine) -> None:
        slots = resample(engine.forecast(hours=25, start=pt(2026, 11, 1)), 30)
        one_am = [s for s in slots if s.start.hour == 1]
        assert len(one_am) == 4  # two hours, two slots each
        assert len({s.start.utcoffset() for s in one_am}) == 2

    def test_spring_forward_never_emits_the_missing_hour(self, engine: RateEngine) -> None:
        slots = resample(engine.forecast(hours=23, start=pt(2027, 3, 14)), 30)
        assert 2 not in {s.start.hour for s in slots if s.start.day == 14}


class TestPredbat:
    @staticmethod
    def _interpreted_rate(entry: dict[str, object]) -> float:
        """Model the two rate shapes accepted by Predbat 8.48.4."""
        if {"from", "to", "rate"} <= entry.keys():
            return float(entry["rate"])
        if {"start", "end", "value"} <= entry.keys():
            return float(entry["value"]) * 100
        raise AssertionError(f"unsupported Predbat rate entry: {entry!r}")

    def test_publishes_both_attributes(self, curve: PriceCurve) -> None:
        attrs = raw_attributes(curve, direction="import", today=date(2026, 7, 15))
        assert set(attrs) == {
            "raw_today", "raw_tomorrow",
            "raw_today_generation", "raw_tomorrow_generation",
            "raw_today_delivery", "raw_tomorrow_delivery",
        }

    def test_entry_shape_is_from_to_rate(self, curve: PriceCurve) -> None:
        entry = raw_attributes(curve, direction="import", today=date(2026, 7, 15))["raw_today"][0]
        assert set(entry) == {"from", "to", "rate"}
        assert datetime.fromisoformat(entry["from"]).utcoffset() is not None

    def test_values_are_cents_not_dollars(self, curve: PriceCurve) -> None:
        attrs = raw_attributes(curve, direction="import", today=date(2026, 7, 15))
        dollars = curve[0].import_price.total
        assert attrs["raw_today"][0]["rate"] == pytest.approx(dollars * CENTS_PER_DOLLAR)
        assert attrs["raw_today"][0]["rate"] > 1  # a cents-scale figure, not 0.33

    def test_predbat_does_not_rescale_rate_shape(self) -> None:
        entry: dict[str, object] = {
            "from": "2026-07-15T00:00:00-07:00",
            "to": "2026-07-15T00:30:00-07:00",
            "rate": 39.026,
        }
        assert self._interpreted_rate(entry) == 39.026

    def test_contract_fixture_detects_legacy_shape_rescaling(self) -> None:
        entry: dict[str, object] = {
            "start": "2026-07-15T00:00:00-07:00",
            "end": "2026-07-15T00:30:00-07:00",
            "value": 39.026,
        }
        assert self._interpreted_rate(entry) == pytest.approx(3902.6)

    def test_scale_is_overridable(self, curve: PriceCurve) -> None:
        attrs = raw_attributes(curve, direction="import", today=date(2026, 7, 15), scale=1.0)
        assert attrs["raw_today"][0]["rate"] == pytest.approx(curve[0].import_price.total)

    def test_export_direction_reads_the_export_side(self, curve: PriceCurve) -> None:
        imports = raw_attributes(curve, direction="import", today=date(2026, 7, 15))
        exports = raw_attributes(curve, direction="export", today=date(2026, 7, 15))
        assert exports["raw_today"][0]["rate"] == pytest.approx(
            curve[0].export_price.total * CENTS_PER_DOLLAR
        )
        assert exports["raw_today"][0]["rate"] != imports["raw_today"][0]["rate"]

    def test_partitions_by_pacific_calendar_date(self, curve: PriceCurve) -> None:
        attrs = raw_attributes(curve, direction="import", today=date(2026, 7, 15))
        assert {datetime.fromisoformat(e["from"]).date() for e in attrs["raw_today"]} == {
            date(2026, 7, 15)
        }
        assert {datetime.fromisoformat(e["from"]).date() for e in attrs["raw_tomorrow"]} == {
            date(2026, 7, 16)
        }

    def test_a_full_day_yields_48_half_hour_entries(self, curve: PriceCurve) -> None:
        attrs = raw_attributes(curve, direction="import", today=date(2026, 7, 15))
        assert len(attrs["raw_today"]) == 48
        assert len(attrs["raw_tomorrow"]) == 48

    def test_entries_are_ordered_and_gapless(self, curve: PriceCurve) -> None:
        entries = raw_attributes(curve, direction="import", today=date(2026, 7, 15))["raw_today"]
        for earlier, later in pairwise(entries):
            assert earlier["to"] == later["from"]

    def test_short_horizon_leaves_tomorrow_empty(self, engine: RateEngine) -> None:
        """How Predbat already represents 'tomorrow is not published yet'."""
        short = engine.forecast(hours=6, start=pt(2026, 7, 15))
        attrs = raw_attributes(short, direction="import", today=date(2026, 7, 15))
        assert attrs["raw_today"] and attrs["raw_tomorrow"] == []

    def test_drops_slots_beyond_tomorrow(self, engine: RateEngine) -> None:
        long = engine.forecast(hours=96, start=pt(2026, 7, 15))
        attrs = raw_attributes(long, direction="import", today=date(2026, 7, 15))
        assert len(attrs["raw_today"]) == 48
        assert len(attrs["raw_tomorrow"]) == 48

    def test_anchor_defaults_to_today_so_a_stale_curve_reports_nothing(
        self, curve: PriceCurve
    ) -> None:
        attrs = raw_attributes(curve, direction="import")
        assert attrs["raw_today"] == [] and attrs["raw_tomorrow"] == []

    def test_fall_back_day_has_50_entries(self, engine: RateEngine) -> None:
        day = engine.forecast(hours=25, start=pt(2026, 11, 1))
        attrs = raw_attributes(day, direction="import", today=date(2026, 11, 1))
        assert len(attrs["raw_today"]) == 50


class TestLocalDayWindow:
    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (pt(2026, 7, 15, 18), 48),
            (pt(2026, 11, 1, 18), 49),  # fall-back day is 25 hours
            (pt(2027, 3, 14, 18), 47),  # spring-forward day is 23
        ],
        ids=["ordinary", "fall-back", "spring-forward"],
    )
    def test_span_follows_real_hours_not_calendar_days(
        self, moment: datetime, expected: int
    ) -> None:
        _, hours = local_day_window(moment, days=2)
        assert hours == expected

    def test_anchors_to_midnight_regardless_of_the_time_of_day(self) -> None:
        midnight, _ = local_day_window(pt(2026, 7, 15, 18))
        assert (midnight.hour, midnight.minute, midnight.date()) == (0, 0, date(2026, 7, 15))

    def test_window_ends_on_a_local_midnight(self, engine: RateEngine) -> None:
        start, hours = local_day_window(pt(2026, 11, 1, 18), days=2)
        curve = engine.forecast(hours, start=start)
        assert curve[-1].end.hour == 0
        assert curve[-1].end.date() == date(2026, 11, 3)


class TestPredbatPayload:
    """The midnight anchoring, which is what makes raw_today complete."""

    def test_both_directions_present(self, engine: RateEngine) -> None:
        assert set(predbat_payload(engine, pt(2026, 7, 15, 18))) == {"import", "export"}

    @pytest.mark.parametrize("hour", [0, 6, 13, 18, 23])
    def test_today_is_a_full_day_whatever_the_time_of_day(
        self, engine: RateEngine, hour: int
    ) -> None:
        """The bug this anchoring exists to prevent.

        Partitioning the coordinator's forecast -- which starts at the current
        hour -- would leave raw_today holding only the remainder of the day, and
        Predbat backfills a short day by copying the previous one.
        """
        out = predbat_payload(engine, pt(2026, 7, 15, hour))
        assert len(out["import"]["raw_today"]) == 48
        assert len(out["import"]["raw_tomorrow"]) == 48

    def test_today_starts_at_midnight_not_the_current_hour(self, engine: RateEngine) -> None:
        out = predbat_payload(engine, pt(2026, 7, 15, 18))
        first = datetime.fromisoformat(out["import"]["raw_today"][0]["from"])
        assert (first.hour, first.minute) == (0, 0)

    def test_today_and_tomorrow_join_without_a_gap(self, engine: RateEngine) -> None:
        out = predbat_payload(engine, pt(2026, 7, 15, 18))
        assert out["import"]["raw_today"][-1]["to"] == out["import"]["raw_tomorrow"][0]["from"]

    def test_covers_the_fall_back_day_completely(self, engine: RateEngine) -> None:
        out = predbat_payload(engine, pt(2026, 11, 1, 18))
        assert len(out["import"]["raw_today"]) == 50

    def test_covers_the_spring_forward_day_completely(self, engine: RateEngine) -> None:
        out = predbat_payload(engine, pt(2027, 3, 14, 18))
        assert len(out["import"]["raw_today"]) == 46

    def test_import_and_export_differ(self, engine: RateEngine) -> None:
        out = predbat_payload(engine, pt(2026, 7, 15, 12))
        assert out["import"]["raw_today"][0]["rate"] != out["export"]["raw_today"][0]["rate"]

    def test_serialises_without_a_custom_encoder(self, engine: RateEngine) -> None:
        """No stray datetime objects left in the payload."""
        json.dumps(predbat_payload(engine, pt(2026, 7, 15, 12)))


class TestEmhass:
    def test_runtime_params_are_bare_lists(self, curve: PriceCurve) -> None:
        """EMHASS reads these positionally; a mapping is not accepted."""
        payload = forecast_lists(curve)
        assert isinstance(payload["load_cost_forecast"], list)
        assert isinstance(payload["prod_price_forecast"], list)
        assert all(isinstance(v, float) for v in payload["load_cost_forecast"])

    def test_carries_the_horizon(self, curve: PriceCurve) -> None:
        payload = forecast_lists(curve)
        assert payload["prediction_horizon"] == len(payload["load_cost_forecast"])
        assert payload["prediction_horizon"] == len(payload["prod_price_forecast"])

    def test_defaults_to_emhass_shipped_time_step(self, curve: PriceCurve) -> None:
        """config_defaults.json ships optimization_time_step: 30, not 60."""
        assert forecast_lists(curve)["prediction_horizon"] == 2 * len(curve)

    def test_honours_an_explicit_time_step(self, curve: PriceCurve) -> None:
        assert forecast_lists(curve, minutes=60)["prediction_horizon"] == len(curve)

    def test_values_stay_in_dollars(self, curve: PriceCurve) -> None:
        payload = forecast_lists(curve)
        assert payload["load_cost_forecast"][0] == pytest.approx(curve[0].import_price.total)

    def test_load_cost_is_import_and_prod_price_is_export(self, curve: PriceCurve) -> None:
        payload = forecast_lists(curve)
        assert payload["prod_price_forecast"][0] == pytest.approx(curve[0].export_price.total)

    def test_list_length_tracks_the_horizon(self, engine: RateEngine) -> None:
        short = engine.forecast(hours=12, start=pt(2026, 7, 15))
        assert forecast_lists(short, minutes=60)["prediction_horizon"] == 12


class TestEmhassAlignment:
    """The lists are positional, so the first value must be the current slot."""

    @pytest.mark.parametrize(
        ("minute", "expected_first"),
        [(0, "00:00"), (15, "00:00"), (30, "00:30"), (45, "00:30")],
    )
    def test_elapsed_slots_are_dropped(
        self, engine: RateEngine, minute: int, expected_first: str
    ) -> None:
        """The engine floors to the hour; EMHASS's timeline floors to 30 minutes."""
        curve = engine.forecast(hours=4, start=pt(2026, 7, 15))
        since = datetime(2026, 7, 15, 0, minute, tzinfo=PACIFIC)
        payload = forecast_payload(curve, since=since)
        assert next(iter(payload["load_cost_forecast"])).endswith(f"T{expected_first}:00-07:00")

    def test_horizon_shrinks_with_the_trim(self, engine: RateEngine) -> None:
        curve = engine.forecast(hours=4, start=pt(2026, 7, 15))
        full = forecast_lists(curve)["prediction_horizon"]
        trimmed = forecast_lists(curve, since=datetime(2026, 7, 15, 1, tzinfo=PACIFIC))
        assert trimmed["prediction_horizon"] == full - 2

    def test_without_since_nothing_is_dropped(self, curve: PriceCurve) -> None:
        assert forecast_lists(curve)["prediction_horizon"] == 2 * len(curve)


class TestEmhassTimestamped:
    def test_keys_are_iso_timestamps_with_an_offset(self, curve: PriceCurve) -> None:
        for key in forecast_payload(curve)["load_cost_forecast"]:
            assert datetime.fromisoformat(key).utcoffset() is not None

    def test_repeated_hour_stays_distinct(self, engine: RateEngine) -> None:
        """A naive %H:%M format would collapse the fall-back day's two 01:00s."""
        day = engine.forecast(hours=25, start=pt(2026, 11, 1))
        assert len(forecast_payload(day)["load_cost_forecast"]) == 50

    def test_matches_the_list_form_ordering(self, curve: PriceCurve) -> None:
        payload = forecast_payload(curve)
        lists = forecast_lists(curve)
        assert list(payload["load_cost_forecast"].values()) == lists["load_cost_forecast"]
