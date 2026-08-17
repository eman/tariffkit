"""Native Home Assistant coverage for the TariffKit custom component."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from custom_components.tariffkit import CONFIG_VERSION, async_migrate_entry, async_setup
from custom_components.tariffkit.const import (
    ATTR_RAW_TODAY,
    ATTR_RAW_TOMORROW,
    CONF_FORECAST_HOURS,
    CONF_PREDBAT_ENABLED,
    CONF_PROFILE,
    DOMAIN,
    SERVICE_GET_EMHASS_FORECAST,
    SERVICE_GET_RATES,
)
from custom_components.tariffkit.coordinator import TariffKitQuality
from custom_components.tariffkit.profile import profile_payload
from custom_components.tariffkit.sensor import TariffKitSensor
from homeassistant.components.energy.validate import (
    ENERGY_PRICE_UNIT_ERROR,
    ENERGY_PRICE_UNITS,
    ValidationIssues,
    _async_validate_price_entity,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceRegistry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tariffkit import Config
from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.models import ExportPrice, ImportPrice, PricePoint
from tariffkit.timeutil import PACIFIC


def _profile_data(*tariffs: tuple[str, date] | str) -> dict[str, object]:
    epochs = tuple(
        AccountEpoch(
            effective=value[1] if isinstance(value, tuple) else date(1970, 1, 1),
            config=Config(tariff=value[0] if isinstance(value, tuple) else value),
        )
        for value in tariffs
    )
    return profile_payload(AccountProfile(epochs))


def _entry(
    *,
    profile: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
    title: str = "Test account",
    version: int = 2,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        version=version,
        data={
            CONF_PROFILE: profile or _profile_data("E-ELEC"),
            CONF_FORECAST_HOURS: 4,
        },
        options=options or {},
    )


@pytest.fixture
def entry() -> MockConfigEntry:
    return _entry()


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_{key}",
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_exposes_energy_price_entities_and_service_device(
    hass: HomeAssistant, entry: MockConfigEntry, device_registry: DeviceRegistry
) -> None:
    await _setup_entry(hass, entry)

    import_state = hass.states.get(_entity_id(hass, entry, "import_price"))
    export_state = hass.states.get(_entity_id(hass, entry, "export_price"))
    assert import_state is not None
    assert export_state is not None
    assert ATTR_RAW_TODAY not in import_state.attributes
    assert ATTR_RAW_TOMORROW not in export_state.attributes
    assert import_state.attributes["unit_of_measurement"] == "USD/kWh"
    assert export_state.attributes["unit_of_measurement"] == "USD/kWh"
    assert import_state.attributes["state_class"] == "measurement"
    assert export_state.attributes["state_class"] == "measurement"
    assert "device_class" not in import_state.attributes
    assert "device_class" not in export_state.attributes

    device = device_registry.async_get_device({(DOMAIN, entry.entry_id)}, set())
    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.manufacturer == "Pacific Gas and Electric Company"
    assert device.configuration_url is None or device.configuration_url.startswith(
        ("http://", "https://")
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_initial_flow_branches_to_manual_import_and_conditional_delivery(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    flow_id = result["flow_id"]
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"manual", "import"}

    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    assert result["step_id"] == "manual"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "profile_name": "import-only",
            "supplier": "bundled",
            "tariff": "E-ELEC",
            "export_enabled": False,
        },
    )
    assert result["step_id"] == "manual_delivery"
    assert "interconnection_year" not in result["data_schema"].schema

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"base_services_charge_tier": 3},
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PROFILE]["name"] == "import-only"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_manual_export_setup_allows_blank_pto_date(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "profile_name": "solar",
            "supplier": "bundled",
            "tariff": "E-ELEC",
            "export_enabled": True,
        },
    )
    assert result["step_id"] == "manual_delivery"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "interconnection_year": "2026",
            "acc_plus_segment": "residential",
            "discount": "none",
            "base_services_charge_tier": 3,
        },
    )
    assert result["type"] == "create_entry"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_manual_cca_rejects_schedule_absent_from_rate_card(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "profile_name": "unsupported-cca",
            "supplier": "cca",
            "tariff": "E-1",
            "export_enabled": False,
        },
    )
    assert result["step_id"] == "manual_delivery"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"base_services_charge_tier": 3},
    )
    assert result["step_id"] == "manual_cca"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "cca_rate_card": "MCE",
            "cca_option": "light_green",
            "cca_pcia_vintage": 2026,
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_config"}
    assert (
        "does not publish generation rates for E-1" in result["description_placeholders"]["detail"]
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_initial_import_and_multiple_entries_are_supported(
    hass: HomeAssistant,
) -> None:
    imported = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="E-ELEC")),),
        name="first-account",
    )
    for name in ("first-account", "second-account"):
        profile = AccountProfile(imported.epochs, name=name)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
        )
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "import"})
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {"profile_json": json.dumps(profile.to_dict())},
        )
        assert result["type"] == "create_entry"
        assert result["title"] == name


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_duplicate_profile_name_is_rejected(hass: HomeAssistant) -> None:
    profile = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="E-ELEC")),),
        name="home",
    )
    for expected_type in ("create_entry", "abort"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
        )
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "import"})
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {"profile_json": json.dumps(profile.to_dict())},
        )
        assert result["type"] == expected_type


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_forecast_entity_is_compact_and_unrecorded(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)

    state = hass.states.get(_entity_id(hass, entry, "forecast_through"))
    assert state is not None
    assert state.attributes["quality"]["complete"] is True
    assert state.attributes["rates"]
    assert set(state.attributes["rates"][0]) == {
        "start",
        "end",
        "import",
        "export",
        "spread",
    }
    assert ATTR_RAW_TODAY in TariffKitSensor._unrecorded_attributes
    assert "rates" in TariffKitSensor._unrecorded_attributes


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostic_entities_explain_forecast_and_rate_data(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)

    registry = er.async_get(hass)
    forecast_id = _entity_id(hass, entry, "forecast_through")
    status_id = _entity_id(hass, entry, "rate_data_status")
    forecast_entry = registry.async_get(forecast_id)
    status_entry = registry.async_get(status_id)
    assert forecast_entry is not None
    assert status_entry is not None
    assert forecast_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert status_entry.entity_category is EntityCategory.DIAGNOSTIC

    status = hass.states.get(status_id)
    assert status is not None
    assert status.state == "current"
    assert status.attributes["pto_date"] == "2026-06-03"
    assert status.attributes["export_rate_lock_end"] == "2035-06-02"
    assert status.attributes["export_vintage"] == "NBT26"
    assert status.attributes["tariff_effective"] == "2026-03-01"
    assert status.attributes["tariff_advice_letter"] == "7846-E"
    assert status.attributes["quality"] == {
        "complete": True,
        "exact": True,
        "locked": True,
    }
    assert status.attributes["source_url"].startswith("https://")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_energy_dashboard_accepts_both_price_entities(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)
    issues = ValidationIssues()
    for key in ("import_price", "export_price"):
        _async_validate_price_entity(
            hass,
            _entity_id(hass, entry, key),
            issues,
            ENERGY_PRICE_UNITS,
            ENERGY_PRICE_UNIT_ERROR,
        )
    assert issues.issues == {}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unload_removes_entities_but_keeps_integration_actions(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_RATES)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id(hass, entry, "import_price"))
    assert state is not None
    assert state.state == "unavailable"
    assert hass.services.has_service(DOMAIN, SERVICE_GET_RATES)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reload_recreates_current_entities(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)
    entity_id = _entity_id(hass, entry, "import_price")
    first_state = hass.states.get(entity_id)
    assert first_state is not None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded_state = hass.states.get(entity_id)
    assert reloaded_state is not None
    assert reloaded_state.state not in {"unknown", "unavailable"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_get_rates_response_supports_bounded_slots_and_rejects_naive_time(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)

    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_RATES,
        {
            "config_entry": entry.entry_id,
            "date": "2026-08-15",
            "horizon": 2,
            "resolution": 30,
        },
        blocking=True,
        return_response=True,
    )
    assert result is not None
    assert len(result["points"]) == 4
    assert result["resolution"] == 30
    assert set(result["quality"]) == {"complete", "exact", "locked"}

    with pytest.raises(HomeAssistantError, match="explicit offset"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_RATES,
            {
                "config_entry": entry.entry_id,
                "start": "2026-08-15T00:00:00",
                "horizon": 1,
            },
            blocking=True,
            return_response=True,
        )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_emhass_response_has_positional_arrays(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)

    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_EMHASS_FORECAST,
        {
            "config_entry": entry.entry_id,
            "date": "2026-08-15",
            "horizon": 2,
            "resolution": 60,
        },
        blocking=True,
        return_response=True,
    )
    assert result is not None
    assert len(result["load_cost_forecast"]) == 2
    assert len(result["prod_price_forecast"]) == 2
    assert result["prediction_horizon"] == 2


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_actions_reject_unloaded_entries(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await async_setup(hass, {})
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_RATES,
            {"config_entry": entry.entry_id, "date": "2026-08-15"},
            blocking=True,
            return_response=True,
        )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_predbat_is_opt_in_and_warns_for_non_pacific_hass(
    hass: HomeAssistant,
) -> None:
    hass.config.time_zone = "UTC"
    entry = _entry(options={CONF_PREDBAT_ENABLED: True})
    await _setup_entry(hass, entry)

    import_state = hass.states.get(_entity_id(hass, entry, "import_price"))
    export_state = hass.states.get(_entity_id(hass, entry, "export_price"))
    assert import_state is not None
    assert export_state is not None
    assert ATTR_RAW_TODAY in import_state.attributes
    assert ATTR_RAW_TOMORROW in import_state.attributes
    assert import_state.attributes["predbat_warning"]
    assert ATTR_RAW_TODAY in export_state.attributes


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_predbat_payload_is_cached_by_day_and_epoch(hass: HomeAssistant) -> None:
    entry = _entry(options={CONF_PREDBAT_ENABLED: True})
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data
    first = coordinator._predbat_for(coordinator.current_hour)
    second = coordinator._predbat_for(coordinator.current_hour)
    assert first is not None
    assert first is second


def test_quality_aggregation_is_conservative() -> None:
    point = PricePoint(
        start=datetime(2026, 1, 1, tzinfo=PACIFIC),
        end=datetime(2026, 1, 1, 1, tzinfo=PACIFIC),
        import_price=ImportPrice(1.0, "summer", "off_peak"),
        export_price=ExportPrice(0.5, "NBT26", "weekday", complete=False, exact=True, locked=False),
    )
    quality = TariffKitQuality.from_points((point,))
    assert quality.to_dict() == {"complete": False, "exact": True, "locked": False}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_are_sanitized(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup_entry(hass, entry)
    from custom_components.tariffkit.diagnostics import async_get_config_entry_diagnostics

    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["loaded"] is True
    assert "quality" in result
    assert "account_profile" not in str(result)
    assert "observations" not in str(result)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_legacy_entry_migration_preserves_pricing(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Legacy account",
        version=1,
        data={
            "tariff": "E-ELEC",
            "supplier": "bundled",
            "interconnection_year": 2026,
            "pto_date": "2026-06-03",
            CONF_FORECAST_HOURS: 6,
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == CONFIG_VERSION
    assert CONF_PROFILE in entry.data
    assert entry.options[CONF_FORECAST_HOURS] == 6


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_menu_groups_account_history(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) >= {"settings", "forecast", "history"}
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "history"})
    assert set(result["menu_options"]) >= {
        "inspect",
        "add_epoch",
        "edit_epoch",
        "remove_epoch",
        "import",
        "export",
    }


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_settings_preserve_import_only_account(hass: HomeAssistant) -> None:
    profile = AccountProfile(
        (
            AccountEpoch(
                date(1970, 1, 1),
                Config(
                    tariff="E-ELEC",
                    interconnection_year=None,
                    pto_date=None,
                    vintage="NBT00",
                ),
            ),
        ),
        name="import-only",
    )
    entry = _entry(profile=profile_payload(profile))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "settings"}
    )
    identity = result["data_schema"]({})
    assert identity["export_enabled"] is False

    result = await hass.config_entries.options.async_configure(flow_id, identity)
    delivery = result["data_schema"]({})
    assert "interconnection_year" not in delivery
    result = await hass.config_entries.options.async_configure(flow_id, delivery)
    assert result["type"] == "create_entry"
    saved = AccountProfile.from_dict(result["data"][CONF_PROFILE])
    assert saved.epochs[0].config.interconnection_year is None
    assert saved.epochs[0].config.vintage == "NBT00"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_settings_abort_for_future_only_profile(hass: HomeAssistant) -> None:
    future = AccountProfile(
        (AccountEpoch(date(2099, 1, 1), Config(tariff="E-ELEC")),),
        name="future",
    )
    entry = _entry(profile=profile_payload(future))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_profile"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_import_branch_replaces_profile(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "history"})
    result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "import"})
    assert result["step_id"] == "import"

    imported = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="EV2-A")),),
        name="",
    )
    result = await hass.config_entries.options.async_configure(
        flow_id,
        {"profile_json": json.dumps(imported.to_dict())},
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PROFILE]["name"] is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_import_rejects_a_different_profile_name(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "history"})
    result = await hass.config_entries.options.async_configure(flow_id, {"next_step_id": "import"})
    imported = AccountProfile(
        (AccountEpoch(date(2026, 1, 1), Config(tariff="EV2-A")),),
        name="other-account",
    )

    result = await hass.config_entries.options.async_configure(
        flow_id,
        {"profile_json": json.dumps(imported.to_dict())},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_profile"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_transition_forecast_crosses_profile_epoch(
    hass: HomeAssistant,
) -> None:
    entry = _entry(
        profile=_profile_data(("E-ELEC", date(2026, 8, 15)), ("EV2-A", date(2026, 8, 16)))
    )
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data
    curve = coordinator.engine.forecast(
        2,
        start=datetime(2026, 8, 15, 23, tzinfo=PACIFIC),
    )
    assert coordinator.engine.describe(curve[0].start)["account_effective"]["tariff"] == "E-ELEC"
    assert coordinator.engine.describe(curve[1].start)["account_effective"]["tariff"] == "EV2-A"
    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_RATES,
        {
            "config_entry": entry.entry_id,
            "start": "2026-08-15T23:00:00-07:00",
            "horizon": 2,
        },
        blocking=True,
        return_response=True,
    )
    assert result is not None
    segments = result["provenance"]["segments"]
    assert [segment["tariff"] for segment in segments] == ["E-ELEC", "EV2-A"]
    assert segments[0]["end"] == segments[1]["start"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_dst_forecast_uses_absolute_hours(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data
    spring = coordinator.engine.forecast(
        24,
        start=datetime(2026, 3, 8, tzinfo=ZoneInfo("America/Los_Angeles")),
    )
    fall = coordinator.engine.forecast(
        24,
        start=datetime(2026, 11, 1, tzinfo=ZoneInfo("America/Los_Angeles")),
    )
    assert len(spring.points) == len(fall.points) == 24
    assert spring.end.astimezone(ZoneInfo("UTC")) - spring.start.astimezone(ZoneInfo("UTC")) == (
        fall.end.astimezone(ZoneInfo("UTC")) - fall.start.astimezone(ZoneInfo("UTC"))
    )
