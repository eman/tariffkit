"""Running cost and credit from metered energy in the Home Assistant component."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
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
from custom_components.tariffkit.energy import (
    Cycle,
    MeterSettings,
    cycle_start,
    resolve_cycle,
    statement_periods,
)
from custom_components.tariffkit.profile import profile_payload
from custom_components.tariffkit.sensor import TariffKitSensor
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from tariffkit import Config
from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.account.model import (
    AccountObservation,
    MeterSource,
    MeterSources,
    ObservedAgreement,
)
from tariffkit.billing import BillingPeriod
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
    """The fallback, used when no statement evidence exists."""
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
    for key in ("grid_import_today", "net_cost_today", "net_cost_cycle"):
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

    delivered = _state(hass, entry, "grid_import_today")
    received = _state(hass, entry, "grid_export_today")
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
    assert float(_state(hass, entry, "grid_import_cycle").state) == pytest.approx(6.5)


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

    assert float(_state(hass, entry, "grid_import_today").state) == pytest.approx(2.0)
    # No export entity means no export questions to answer.
    assert _entity_id(hass, entry, "export_credit_today") is None
    assert _entity_id(hass, entry, "grid_export_today") is None
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
    assert float(_state(hass, entry, "grid_import_today").state) == pytest.approx(3.0)


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
    assert float(_state(hass, entry, "grid_import_today").state) == pytest.approx(1.0)

    # The counter moves inside the hour, with no new statistic behind it.
    hass.states.async_set(
        IMPORT_ENTITY, "101.75", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    freezer.move_to(NOW.replace(minute=33))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert float(_state(hass, entry, "grid_import_today").state) == pytest.approx(1.75)
    # A counter that goes backwards is a reset, not negative energy.
    hass.states.async_set(
        IMPORT_ENTITY, "0.5", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    freezer.move_to(NOW.replace(minute=36))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert float(_state(hass, entry, "grid_import_today").state) == pytest.approx(1.0)


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

    # Rewritten every minute across six entities, so neither the breakdown nor
    # the fixed prose beside it may reach the database.
    assert "buckets" in TariffKitSensor._unrecorded_attributes
    assert "description" in TariffKitSensor._unrecorded_attributes
    assert _state(hass, entry, "net_cost_today").attributes["buckets"]


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_a_cycle_the_account_history_predates_says_why_it_is_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A cycle opening before the profile's first epoch is refused, not guessed.

    Pricing only the days the history covers would report a smaller number that
    looks complete, so the bill is withheld -- but the entity has to say what
    stopped it rather than reading a bare ``unknown``.
    """
    freezer.move_to(NOW)
    profile = AccountProfile(
        (AccountEpoch(date(2026, 8, 16), Config(tariff="E-ELEC")),),
        name="late-history",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Late history",
        version=CONFIG_VERSION,
        data={CONF_PROFILE: profile_payload(profile), CONF_FORECAST_HOURS: 4},
        # The cycle opens 2026-08-12, four days before the profile begins.
        options=_meter_options(**{CONF_CYCLE_START_DAY: 12}),
    )
    await _setup(hass, entry)

    cycle = _state(hass, entry, "net_cost_cycle")
    assert cycle.state == "unknown"
    assert cycle.attributes["quality"]["complete"] is False
    assert any("cannot price 2026-08-12" in w for w in cycle.attributes["warnings"])

    # The day is inside the history, so it still prices normally.
    assert _state(hass, entry, "net_cost_today").state not in ("unknown", "unavailable")


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_metered_energy_is_configured_only_after_setup(hass: HomeAssistant) -> None:
    """Setup never asks about meters; Configure is the only way in.

    Pricing an account does not require a meter, so making setup mention one
    puts a question in front of every new user that most of them cannot answer
    yet -- the counters are often integrated after the tariff, not before.
    """
    from custom_components.tariffkit.config_flow import (
        TariffKitConfigFlow,
        TariffKitOptionsFlow,
    )

    assert not hasattr(TariffKitConfigFlow, "async_step_meters")
    assert hasattr(TariffKitOptionsFlow, "async_step_meters")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "profile_name": "no-meters",
            "supplier": "bundled",
            "tariff": "E-ELEC",
            "export_enabled": False,
        },
    )
    assert result["step_id"] == "manual_delivery"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"base_services_charge_tier": 3}
    )
    # Straight to the entry: no meters step, and nothing written that would
    # make the running-total entities appear.
    assert result["type"] == "create_entry"
    assert result.get("options", {}) == {}
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.runtime_data.meters.configured is False
    for key in ("grid_import_today", "net_cost_today", "net_cost_cycle"):
        assert _entity_id(hass, entry, key) is None

    # And the options menu is where it does appear.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "meters" in result["menu_options"]


def _period(start: tuple[int, int, int], end: tuple[int, int, int]) -> BillingPeriod:
    return BillingPeriod(date(*start), date(*end))


def test_statement_evidence_beats_a_guessed_meter_read_day() -> None:
    """Real cycles do not open on a fixed day, so evidence wins where it exists.

    PG&E reads on business days, so consecutive cycles on one real account
    opened on the 29th, the 30th, the 1st and the 3rd. Any fixed day of the
    month is therefore wrong for most of them.
    """
    periods = [_period((2026, 6, 1), (2026, 6, 29)), _period((2026, 6, 30), (2026, 7, 28))]

    # Inside a billed cycle: that cycle's own start, exactly.
    assert resolve_cycle(date(2026, 7, 10), 30, periods) == Cycle(date(2026, 6, 30), "statement")

    # After the last statement: cycles are contiguous, so the open one began
    # the day after it ended -- derivable without waiting to be billed.
    assert resolve_cycle(date(2026, 8, 24), 30, periods) == Cycle(date(2026, 7, 29), "statement")

    # The guess would have been a day out, and the calendar month three.
    assert cycle_start(date(2026, 8, 24), 30) == date(2026, 7, 30)
    assert cycle_start(date(2026, 8, 24), 0) == date(2026, 8, 1)


def test_stale_evidence_falls_back_rather_than_inventing_a_long_cycle() -> None:
    """Evidence older than a cycle cannot fix the current boundary.

    A statement has been issued that the profile never imported, so the next
    boundary is not derivable. Trusting the old one would report a 90-day
    "cycle" and charge Base Services Charge for every day of it.
    """
    periods = [_period((2026, 6, 30), (2026, 7, 28))]
    assert resolve_cycle(date(2026, 8, 24), 30, periods).source == "statement"
    stale = resolve_cycle(date(2026, 10, 1), 30, periods)
    assert stale.source == "day_of_month"
    assert stale.start == date(2026, 9, 30)


def test_statement_periods_follow_the_bill_not_the_agreement() -> None:
    """A cycle split by interconnection is one billing period, not two."""
    profile = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="E-ELEC")),),
        name="split",
        observations=(
            AccountObservation(
                agreements=(
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 7, 7),
                        period=_period((2026, 6, 1), (2026, 6, 2)),
                        tariff="EV2-A",
                    ),
                    ObservedAgreement(
                        provider="pge",
                        statement_date=date(2026, 7, 7),
                        period=_period((2026, 6, 3), (2026, 6, 29)),
                        tariff="E-ELEC",
                    ),
                ),
            ),
        ),
    )
    (period,) = statement_periods(profile)
    assert (period.start, period.end) == (date(2026, 6, 1), date(2026, 6, 29))


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_cycle_entity_says_where_its_boundary_came_from(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = _entry(_meter_options(**{CONF_CYCLE_START_DAY: 30}))
    await _setup(hass, entry)

    cycle = _state(hass, entry, "net_cost_cycle")
    # This profile carries no statement evidence, so it says so rather than
    # implying the period matches a bill.
    assert cycle.attributes["cycle_boundary"] == "day_of_month"
    assert cycle.attributes["period_start"] == "2026-07-30"
    # The daily entity has no cycle boundary to report.
    assert "cycle_boundary" not in _state(hass, entry, "net_cost_today").attributes


def test_the_fall_back_hour_is_two_distinct_slots() -> None:
    """Two aware datetimes in one zone compare by wall clock, ignoring fold.

    On the November fall-back Sunday 01:00 PDT and 01:00 PST are an hour apart
    and `==` each other (PEP 495), so keying hourly statistics by a Pacific
    datetime silently drops one of them -- and the energy with it, for the rest
    of the cycle. Keying by epoch seconds cannot collide.
    """
    pdt = datetime(2025, 11, 2, 1, 0, tzinfo=PACIFIC, fold=0)
    pst = datetime(2025, 11, 2, 1, 0, tzinfo=PACIFIC, fold=1)
    assert pst.timestamp() - pdt.timestamp() == 3600.0
    assert pdt == pst, "the collision this guards against still exists in Python"

    by_datetime = {pdt: 1.0, pst: 1.0}
    by_epoch = {pdt.timestamp(): 1.0, pst.timestamp(): 1.0}
    assert len(by_datetime) == 1, "datetime keys collide"
    assert len(by_epoch) == 2, "epoch keys must not"


def test_evidence_never_implies_a_cycle_longer_than_a_real_one() -> None:
    """A cycle runs 27-33 days; the staleness bound must refuse before 34."""
    periods = [_period((2026, 6, 30), (2026, 7, 28))]
    spans = {}
    for day in (date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1)):
        cycle = resolve_cycle(day, 30, periods)
        spans[day] = ((day - cycle.start).days + 1, cycle.source)
    assert spans[date(2026, 8, 29)] == (32, "statement")
    assert spans[date(2026, 8, 30)] == (33, "statement")
    # Beyond a real cycle, so the evidence is stale and it says so.
    assert spans[date(2026, 8, 31)][1] == "day_of_month"
    assert all(span <= 33 for span, source in spans.values() if source == "statement")


def test_one_entity_cannot_be_both_directions() -> None:
    """Naming one counter twice would bill each hour and credit it at once."""
    from custom_components.tariffkit.config_flow import _meter_problem

    class _Hass:
        states = SimpleNamespace(get=lambda _e: None)

    problem = _meter_problem(
        _Hass(),
        {CONF_GRID_IMPORT_ENTITY: IMPORT_ENTITY, CONF_GRID_EXPORT_ENTITY: IMPORT_ENTITY},
    )
    assert "both directions" in problem


def test_a_measurement_sensor_is_rejected_as_a_counter() -> None:
    """`device_class: energy` is not enough; no selector can filter state_class."""
    from custom_components.tariffkit.config_flow import _meter_problem

    class _Hass:
        states = SimpleNamespace(
            get=lambda _e: SimpleNamespace(attributes={"state_class": "measurement"})
        )

    assert "state_class" in _meter_problem(_Hass(), {CONF_GRID_IMPORT_ENTITY: IMPORT_ENTITY})


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_narrowing_the_meters_removes_the_entities_it_no_longer_creates(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Otherwise they linger forever as `unavailable` with no way back."""
    freezer.move_to(NOW)
    entry = _entry(_meter_options())
    await _setup(hass, entry)
    assert _entity_id(hass, entry, "export_credit_today") is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_GRID_EXPORT_ENTITY: ""}
    )
    await hass.async_block_till_done()

    for gone in (
        "export_credit_today",
        "export_credit_cycle",
        "grid_export_today",
        "grid_export_cycle",
    ):
        assert _entity_id(hass, entry, gone) is None, f"{gone} was left behind"
    # The import side, and every rate entity, survive untouched.
    assert _entity_id(hass, entry, "grid_import_today") is not None
    assert _entity_id(hass, entry, "export_price") is not None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_without_a_recorder_the_entities_say_so(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """No `recorder_mock` here, so the recorder is genuinely absent."""
    freezer.move_to(NOW)
    entry = _entry(_meter_options())
    await _setup(hass, entry)

    state = _state(hass, entry, "net_cost_today")
    assert state.state == "unknown"
    assert state.attributes["quality"]["complete"] is False
    assert any("recorder" in w for w in state.attributes["warnings"])
    # And the rate entities are entirely unaffected.
    assert _state(hass, entry, "import_price").state not in ("unknown", "unavailable")


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_bank_entity_appears_only_with_an_export_meter(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """No export meter means no export credit, so there is no bank to carry."""
    freezer.move_to(NOW)
    entry = _entry(_meter_options(**{CONF_GRID_EXPORT_ENTITY: ""}))
    await _setup(hass, entry)
    assert _entity_id(hass, entry, "export_credit_bank") is None

    both = _entry(_meter_options())
    await _setup(hass, both)
    assert _entity_id(hass, both, "export_credit_bank") is not None


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_bank_says_why_it_has_no_figure_yet(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An unexplained `unknown` is the failure this integration keeps fixing."""
    freezer.move_to(NOW)
    entry = _entry(_meter_options())
    await _setup(hass, entry)

    state = _state(hass, entry, "export_credit_bank")
    assert state.state == "unknown"
    assert state.attributes["quality"]["complete"] is False
    assert state.attributes["warnings"], "an empty bank must explain itself"
    assert state.attributes["unit_of_measurement"] == "USD"
    # A balance is a stock, not an accumulator: recording each fall as a
    # negative contribution to a lifetime sum would mean nothing.
    assert state.attributes["state_class"] == "measurement"
    assert "device_class" not in state.attributes


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_net_cost_is_what_a_statement_would_charge_not_the_bill_total(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A cycle that earns more credit than it owes still owes something.

    ``Bill.total`` subtracts every credit the cycle earned and goes negative,
    which no statement has ever printed. The tariff offsets credit only against
    the charges it may offset and banks the remainder, so the entity reports
    what :func:`tariffkit.billing.apply_credits` leaves due and says separately
    how much banked. Getting this wrong is not a rounding difference: it is the
    difference between a bill and a refund.
    """
    freezer.move_to(NOW)
    seed = datetime(2026, 8, 1, tzinfo=PACIFIC)
    await _record(hass, IMPORT_ENTITY, [(seed, 1000.0), (NOW.replace(hour=13, minute=0), 1000.5)])
    await _record(
        hass,
        EXPORT_ENTITY,
        [(seed, 500.0)]
        + [
            (datetime(2026, 8, day, hour, tzinfo=PACIFIC), 500.0 + (day - 1) * 240.0 + hour * 10.0)
            for day in range(1, 25)
            for hour in range(24)
            if (day, hour) <= (NOW.day, 13)
        ],
    )
    hass.states.async_set(
        IMPORT_ENTITY, "1000.5", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    hass.states.async_set(
        EXPORT_ENTITY, "6050.0", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    await hass.async_block_till_done()

    entry = _entry(_meter_options())
    await _setup(hass, entry)

    cycle = _state(hass, entry, "net_cost_cycle")
    credit = float(_state(hass, entry, "export_credit_cycle").state)
    cost = float(_state(hass, entry, "energy_cost_cycle").state)
    fixed = cycle.attributes["fixed_charges"]

    assert credit > cost + fixed, "the premise: more credit earned than charges to spend it on"
    # So the bill's own arithmetic would hand back money...
    assert cost + fixed - credit < 0
    # ...and the entity does not. It reports the charges no credit could reach.
    assert float(cycle.state) >= 0
    assert cycle.attributes["banked"] > 0
    assert cycle.attributes["export_credits"] == pytest.approx(credit, abs=1e-4)
