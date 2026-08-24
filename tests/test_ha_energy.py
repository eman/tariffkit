"""Running cost and credit from metered energy in the Home Assistant component."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from custom_components.tariffkit import CONFIG_VERSION
from custom_components.tariffkit.const import (
    CONF_CYCLE_START_DAY,
    CONF_FORECAST_HOURS,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_PROFILE,
    DOMAIN,
)
from custom_components.tariffkit.energy import MeterSettings, cycle_start
from custom_components.tariffkit.profile import profile_payload
from custom_components.tariffkit.sensor import TariffKitSensor
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from tariffkit import Config
from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.account.model import MeterSource, MeterSources
from tariffkit.timeutil import PACIFIC

IMPORT_ENTITY = "sensor.grid_energy_delivered"
EXPORT_ENTITY = "sensor.grid_energy_received"
#: Mid-afternoon on a summer weekday, so the priced hours span more than one
#: time-of-use period and the day's buckets are not all the same rate.
NOW = datetime(2026, 8, 24, 14, 30, tzinfo=PACIFIC)


def _entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    profile = AccountProfile(
        (AccountEpoch(date(1970, 1, 1), Config(tariff="E-ELEC", pto_date=date(2026, 1, 1))),),
        name="metered",
    )
    return MockConfigEntry(
        domain=DOMAIN,
        title="Metered account",
        version=CONFIG_VERSION,
        data={CONF_PROFILE: profile_payload(profile), CONF_FORECAST_HOURS: 4},
        options=options or {},
    )


def _meter_options(**overrides: Any) -> dict[str, Any]:
    return {
        CONF_GRID_IMPORT_ENTITY: IMPORT_ENTITY,
        CONF_GRID_EXPORT_ENTITY: EXPORT_ENTITY,
        CONF_CYCLE_START_DAY: 0,
        **overrides,
    }


def _stats(entity: str, unit: str = "kWh") -> dict[str, Any]:
    return {
        "mean_type": StatisticMeanType.NONE,
        "has_mean": False,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": entity,
        "unit_class": "energy",
        "unit_of_measurement": unit,
    }


async def _record(hass: HomeAssistant, entity: str, points: list[tuple[datetime, float]]) -> None:
    """Write hourly statistics as the recorder itself would have compiled them."""
    async_import_statistics(
        hass,
        _stats(entity),
        [
            {"start": start.astimezone(PACIFIC), "state": total, "sum": total}
            for start, total in points
        ],
    )
    await async_wait_recording_done(hass)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(Platform.SENSOR, DOMAIN, f"{entry.entry_id}_{key}")


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> State:
    entity_id = _entity_id(hass, entry, key)
    assert entity_id is not None, f"{key} entity was never created"
    state = hass.states.get(entity_id)
    assert state is not None
    return state


@pytest.mark.parametrize(
    ("day", "start_day", "expected"),
    [
        (date(2026, 8, 24), 0, date(2026, 8, 1)),
        (date(2026, 8, 24), 12, date(2026, 8, 12)),
        (date(2026, 8, 3), 12, date(2026, 7, 12)),
        (date(2026, 8, 12), 12, date(2026, 8, 12)),
        # A read day past the end of a short month clamps rather than skipping
        # a cycle or raising.
        (date(2026, 3, 15), 31, date(2026, 2, 28)),
        (date(2026, 5, 3), 31, date(2026, 4, 30)),
    ],
)
def test_cycle_start_clamps_to_the_month(day: date, start_day: int, expected: date) -> None:
    assert cycle_start(day, start_day) == expected


def test_meter_settings_fall_back_to_an_imported_profile_mapping() -> None:
    profile = AccountProfile(
        (AccountEpoch(date(1970, 1, 1), Config()),),
        name="imported",
        meter_sources=MeterSources(
            ha=MeterSource(
                grid_import_entity=IMPORT_ENTITY,
                grid_export_entity=EXPORT_ENTITY,
            )
        ),
    )
    assert MeterSettings.from_entry({}, profile).import_entity == IMPORT_ENTITY

    # A key present and empty is a deliberate clearing, not an absent setting.
    cleared = MeterSettings.from_entry({CONF_GRID_IMPORT_ENTITY: ""}, profile)
    assert cleared.import_entity is None
    assert cleared.export_entity == EXPORT_ENTITY


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_no_meters_configured_creates_no_usage_entities(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)

    assert _entity_id(hass, entry, "import_price") is not None
    for key in ("energy_delivered_today", "net_cost_today", "net_cost_cycle"):
        assert _entity_id(hass, entry, key) is None


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_running_totals_price_metered_hours(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    seed = datetime(2026, 7, 31, 23, tzinfo=PACIFIC)
    await _record(
        hass,
        IMPORT_ENTITY,
        [
            (seed, 1000.0),
            (NOW.replace(hour=9, minute=0), 1002.0),
            (NOW.replace(hour=10, minute=0), 1005.0),
            (NOW.replace(hour=13, minute=0), 1006.0),
        ],
    )
    await _record(
        hass,
        EXPORT_ENTITY,
        [(seed, 500.0), (NOW.replace(hour=10, minute=0), 504.0)],
    )
    hass.states.async_set(
        IMPORT_ENTITY, "1006.5", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    hass.states.async_set(
        EXPORT_ENTITY, "504.0", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    await hass.async_block_till_done()

    entry = _entry(_meter_options())
    await _setup(hass, entry)

    delivered = _state(hass, entry, "energy_delivered_today")
    received = _state(hass, entry, "energy_received_today")
    # Three completed hours plus the counter's advance inside the current one.
    assert float(delivered.state) == pytest.approx(6.5)
    assert float(received.state) == pytest.approx(4.0)
    assert delivered.attributes["unit_of_measurement"] == "kWh"
    assert delivered.attributes["source_entity"] == IMPORT_ENTITY
    assert delivered.attributes["last_reset"] == datetime(2026, 8, 24, tzinfo=PACIFIC).isoformat()

    cost = _state(hass, entry, "energy_cost_today")
    credit = _state(hass, entry, "export_credit_today")
    net = _state(hass, entry, "net_cost_today")
    assert cost.attributes["unit_of_measurement"] == "USD"
    assert cost.attributes["device_class"] == "monetary"
    assert float(cost.state) > 0
    assert float(credit.state) > 0

    # The day's whole Base Services Charge is in the net, and nothing else is:
    # net is charges plus tax, less credits, plus the fixed charge.
    breakdown = net.attributes
    assert breakdown["days"] == 1
    assert breakdown["fixed_charges"] == pytest.approx(
        float(_state(hass, entry, "daily_fixed_charge").state), abs=1e-4
    )
    assert float(net.state) == pytest.approx(
        breakdown["energy_charges"]
        + breakdown["taxes"]
        - breakdown["export_credits"]
        + breakdown["fixed_charges"],
        abs=1e-4,
    )
    assert breakdown["buckets"]

    # The cycle covers the same readings over more days, so it owes at least
    # the day does and carries more Base Services Charge.
    cycle = _state(hass, entry, "net_cost_cycle")
    assert cycle.attributes["days"] == 24
    assert cycle.attributes["period_start"] == "2026-08-01"
    assert float(cycle.state) > float(net.state)
    assert float(_state(hass, entry, "energy_delivered_cycle").state) == pytest.approx(6.5)


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_counter_restart_does_not_charge_for_a_whole_series(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A statistics series that restarts reports its total as one hour's change."""
    freezer.move_to(NOW)
    seed = datetime(2026, 7, 31, 23, tzinfo=PACIFIC)
    await _record(
        hass,
        IMPORT_ENTITY,
        [
            (seed, 10.0),
            (NOW.replace(hour=9, minute=0), 12.0),
            # 500 kWh in one hour is ten times a 200 A service's ceiling.
            (NOW.replace(hour=10, minute=0), 512.0),
        ],
    )
    hass.states.async_set(
        IMPORT_ENTITY, "512.0", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    await hass.async_block_till_done()

    entry = _entry(_meter_options(**{CONF_GRID_EXPORT_ENTITY: ""}))
    await _setup(hass, entry)

    assert float(_state(hass, entry, "energy_delivered_today").state) == pytest.approx(2.0)
    # No export entity means no export questions to answer.
    assert _entity_id(hass, entry, "export_credit_today") is None
    assert _entity_id(hass, entry, "energy_received_today") is None
    assert _entity_id(hass, entry, "net_cost_today") is not None


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_energy_recorded_in_watt_hours_is_converted(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    seed = datetime(2026, 7, 31, 23, tzinfo=PACIFIC)
    async_import_statistics(
        hass,
        _stats(IMPORT_ENTITY, unit="Wh"),
        [
            {"start": seed, "state": 1_000_000.0, "sum": 1_000_000.0},
            {
                "start": NOW.replace(hour=9, minute=0),
                "state": 1_002_000.0,
                "sum": 1_002_000.0,
            },
        ],
    )
    await async_wait_recording_done(hass)
    hass.states.async_set(
        IMPORT_ENTITY, "1003000", {"unit_of_measurement": "Wh", "device_class": "energy"}
    )
    await hass.async_block_till_done()

    entry = _entry(_meter_options(**{CONF_GRID_EXPORT_ENTITY: ""}))
    await _setup(hass, entry)

    # 2 kWh of completed hours, plus 1 kWh the counter has moved since.
    assert float(_state(hass, entry, "energy_delivered_today").state) == pytest.approx(3.0)


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_missing_statistics_are_reported_rather_than_priced(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = _entry(_meter_options())
    await _setup(hass, entry)

    net = _state(hass, entry, "net_cost_today")
    assert net.attributes["quality"]["complete"] is False
    assert any(IMPORT_ENTITY in warning for warning in net.attributes["warnings"])
    # Nothing metered still owes the day's Base Services Charge.
    assert float(net.state) == pytest.approx(net.attributes["fixed_charges"], abs=1e-4)


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_options_flow_configures_and_clears_meters(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "meters"})
    assert result["step_id"] == "meters"

    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_GRID_IMPORT_ENTITY: IMPORT_ENTITY,
            CONF_GRID_EXPORT_ENTITY: EXPORT_ENTITY,
            CONF_CYCLE_START_DAY: 12,
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_GRID_IMPORT_ENTITY] == IMPORT_ENTITY
    assert entry.options[CONF_CYCLE_START_DAY] == 12
    assert entry.runtime_data.meters.cycle_start_day == 12
    assert _entity_id(hass, entry, "net_cost_cycle") is not None

    # Clearing both entities takes the running totals back out of service.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "meters"})
    result = await hass.config_entries.options.async_configure(flow_id, {CONF_CYCLE_START_DAY: 0})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.meters.configured is False


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_hour_in_progress_tracks_the_counter_between_compiles(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Statistics compile hourly; the running total must not wait for them."""
    freezer.move_to(NOW)
    await _record(
        hass,
        IMPORT_ENTITY,
        [
            (datetime(2026, 7, 31, 23, tzinfo=PACIFIC), 100.0),
            (NOW.replace(hour=13, minute=0), 101.0),
        ],
    )
    hass.states.async_set(
        IMPORT_ENTITY, "101.0", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    await hass.async_block_till_done()

    entry = _entry(_meter_options(**{CONF_GRID_EXPORT_ENTITY: ""}))
    await _setup(hass, entry)
    assert float(_state(hass, entry, "energy_delivered_today").state) == pytest.approx(1.0)

    # The counter moves inside the hour, with no new statistic behind it.
    hass.states.async_set(
        IMPORT_ENTITY, "101.75", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    freezer.move_to(NOW.replace(minute=33))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert float(_state(hass, entry, "energy_delivered_today").state) == pytest.approx(1.75)
    # A counter that goes backwards is a reset, not negative energy.
    hass.states.async_set(
        IMPORT_ENTITY, "0.5", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    freezer.move_to(NOW.replace(minute=36))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert float(_state(hass, entry, "energy_delivered_today").state) == pytest.approx(1.0)


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_money_entities_explain_themselves_without_bloating_the_recorder(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await _record(
        hass,
        IMPORT_ENTITY,
        [
            (datetime(2026, 7, 31, 23, tzinfo=PACIFIC), 0.0),
            (NOW.replace(hour=13, minute=0), 3.0),
        ],
    )
    await _record(
        hass,
        EXPORT_ENTITY,
        [(datetime(2026, 7, 31, 23, tzinfo=PACIFIC), 0.0), (NOW.replace(hour=10, minute=0), 2.0)],
    )
    await hass.async_block_till_done()

    entry = _entry(_meter_options())
    await _setup(hass, entry)

    for key in ("energy_cost_today", "export_credit_today", "net_cost_today"):
        assert _state(hass, entry, key).attributes["description"]
    assert "cycle" in _state(hass, entry, "net_cost_cycle").attributes["description"]

    # The time-of-use breakdown is rewritten every minute on six entities, so
    # it must not reach the database.
    assert "buckets" in TariffKitSensor._unrecorded_attributes
    assert _state(hass, entry, "net_cost_today").attributes["buckets"]
