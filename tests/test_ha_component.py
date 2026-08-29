"""Native Home Assistant coverage for the TariffKit custom component."""

from __future__ import annotations

import json
import threading
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
from freezegun.api import FrozenDateTimeFactory
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
from tariffkit.components import EXPORT_GROUPS, IMPORT_GROUPS
from tariffkit.config import CcaConfig
from tariffkit.models import ExportPrice, ImportPrice, PricePoint, Supplier
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
    # Metered energy is not asked during setup; it lives under Configure only,
    # so a fresh entry carries no meter options at all.
    assert result.get("options", {}) == {}


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
        "import_components",
        "export_components",
    }
    assert ATTR_RAW_TODAY in TariffKitSensor._unrecorded_attributes
    assert "rates" in TariffKitSensor._unrecorded_attributes


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_forecast_carries_stackable_component_groups(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Every forecast hour breaks down into groups that sum back to its price."""
    await _setup_entry(hass, entry)

    state = hass.states.get(_entity_id(hass, entry, "forecast_through"))
    assert state is not None
    for point in state.attributes["rates"]:
        assert set(point["import_components"]) == {str(group) for group in IMPORT_GROUPS}
        assert set(point["export_components"]) == {str(group) for group in EXPORT_GROUPS}
        assert sum(point["import_components"].values()) == pytest.approx(point["import"])
        assert sum(point["export_components"].values()) == pytest.approx(point["export"])


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_component_entities_stack_to_the_price(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The group entities exist for every group and add up to their direction."""
    await _setup_entry(hass, entry)

    for direction, groups in (("import", IMPORT_GROUPS), ("export", EXPORT_GROUPS)):
        price = hass.states.get(_entity_id(hass, entry, f"{direction}_price"))
        assert price is not None
        stack = 0.0
        for group in groups:
            state = hass.states.get(_entity_id(hass, entry, f"{direction}_{group}"))
            assert state is not None, f"missing {direction} {group} entity"
            assert state.attributes["unit_of_measurement"] == "USD/kWh"
            assert state.attributes["direction"] == direction
            stack += float(state.state)
        assert stack == pytest.approx(float(price.state))

    generation = hass.states.get(_entity_id(hass, entry, "import_generation"))
    assert generation is not None
    # The tariff's own lines stay visible behind the roll-up.
    assert "generation" in generation.attributes["components"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_daily_fixed_charge_is_reported_per_day(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The Base Services Charge is exposed, in USD/day, outside the price stack."""
    await _setup_entry(hass, entry)

    state = hass.states.get(_entity_id(hass, entry, "daily_fixed_charge"))
    assert state is not None
    assert float(state.state) > 0
    assert state.attributes["unit_of_measurement"] == "USD/day"
    # Energy dashboard price validation must not accept a per-day amount.
    assert "USD/day" not in ENERGY_PRICE_UNITS


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


def _cca_profile(name: str = "MCE", option: str = "light_green") -> dict[str, object]:
    """A profile whose generation comes from a CCA rather than the utility."""
    return profile_payload(
        AccountProfile(
            (
                AccountEpoch(
                    effective=date(1970, 1, 1),
                    config=Config(
                        tariff="E-ELEC",
                        supplier=Supplier.CCA,
                        cca=CcaConfig(name=name, rate_card="mce", option=option),
                    ),
                ),
            )
        )
    )


class TestGenerationSupplierOnDevice:
    """The device model names the CCA, while the utility stays the manufacturer.

    PG&E delivers either way, so replacing the manufacturer would misstate who
    runs the wires; the CCA belongs to the rate identity instead.
    """

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_bundled_account_names_no_generation_supplier(
        self, hass: HomeAssistant, device_registry: DeviceRegistry
    ) -> None:
        entry = _entry()
        await _setup_entry(hass, entry)

        device = device_registry.async_get_device({(DOMAIN, entry.entry_id)}, set())
        assert device is not None
        assert device.model is not None
        assert "·" not in device.model
        assert device.manufacturer == "Pacific Gas and Electric Company"

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_cca_account_names_the_cca_and_its_product(
        self, hass: HomeAssistant, device_registry: DeviceRegistry
    ) -> None:
        entry = _entry(profile=_cca_profile())
        await _setup_entry(hass, entry)

        device = device_registry.async_get_device({(DOMAIN, entry.entry_id)}, set())
        assert device is not None
        assert device.model is not None
        assert device.model.endswith("· MCE Light Green")
        # The utility still delivers, so it keeps the manufacturer slot.
        assert device.manufacturer == "Pacific Gas and Electric Company"

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_unnamed_cca_falls_back_to_the_rate_card(
        self, hass: HomeAssistant, device_registry: DeviceRegistry
    ) -> None:
        entry = _entry(profile=_cca_profile(name=""))
        await _setup_entry(hass, entry)

        device = device_registry.async_get_device({(DOMAIN, entry.entry_id)}, set())
        assert device is not None
        assert device.model is not None
        assert device.model.endswith("· MCE Light Green")


class TestGenerationSupplierInServiceProvenance:
    """get_rates names the CCA too.

    The generation component of a CCA price comes from the CCA's rate card, so
    a caller reading that number needs to know whose card it was.
    """

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_cca_appears_in_the_response_provenance(self, hass: HomeAssistant) -> None:
        entry = _entry(profile=_cca_profile())
        await _setup_entry(hass, entry)

        result = await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_RATES,
            {"config_entry": entry.entry_id, "date": "2026-08-15", "horizon": 1},
            blocking=True,
            return_response=True,
        )
        assert result is not None
        segment = result["provenance"]["segments"][0]
        assert segment["cca_name"] == "MCE"
        assert segment["cca_rate_card"] == "mce"
        assert segment["cca_option"] == "light_green"
        # The utility still delivers, so it is not displaced by the CCA.
        assert segment["utility"] == "pacific_gas_and_electric"

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_bundled_account_reports_no_cca(self, hass: HomeAssistant) -> None:
        entry = _entry()
        await _setup_entry(hass, entry)

        result = await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_RATES,
            {"config_entry": entry.entry_id, "date": "2026-08-15", "horizon": 1},
            blocking=True,
            return_response=True,
        )
        assert result is not None
        assert result["provenance"]["segments"][0]["cca_name"] is None


class TestDeviceIdentityFollowsTheProfile:
    """DeviceInfo is read once at registration, so epoch changes need a sync."""

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_switching_to_a_cca_epoch_updates_the_device_model(
        self, hass: HomeAssistant, device_registry: DeviceRegistry, freezer: FrozenDateTimeFactory
    ) -> None:
        profile = profile_payload(
            AccountProfile(
                (
                    AccountEpoch(
                        effective=date(1970, 1, 1),
                        config=Config(tariff="E-ELEC"),
                    ),
                    AccountEpoch(
                        effective=date(2026, 9, 1),
                        config=Config(
                            tariff="E-ELEC",
                            supplier=Supplier.CCA,
                            cca=CcaConfig(name="MCE", rate_card="mce"),
                        ),
                    ),
                )
            )
        )
        entry = _entry(profile=profile)
        freezer.move_to("2026-08-15T12:00:00-07:00")
        await _setup_entry(hass, entry)

        device = device_registry.async_get_device({(DOMAIN, entry.entry_id)}, set())
        assert device is not None and device.model is not None
        assert "MCE" not in device.model

        # Cross into the CCA epoch. Without the registry sync the model would
        # keep naming the old identity until the entry was reloaded.
        freezer.move_to("2026-09-15T12:00:00-07:00")
        coordinator = entry.runtime_data
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        device = device_registry.async_get_device({(DOMAIN, entry.entry_id)}, set())
        assert device is not None and device.model is not None
        assert device.model.endswith("· MCE Light Green")


class TestPermissionToOperateSensors:
    """PTO and the lock end were attributes only; they get their own rows now."""

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_dates_are_published_as_diagnostic_sensors(self, hass: HomeAssistant) -> None:
        profile = profile_payload(
            AccountProfile(
                (
                    AccountEpoch(
                        effective=date(1970, 1, 1),
                        config=Config(tariff="E-ELEC", pto_date=date(2026, 6, 3)),
                    ),
                )
            )
        )
        entry = _entry(profile=profile)
        await _setup_entry(hass, entry)

        pto = hass.states.get(_entity_id(hass, entry, "pto_date"))
        assert pto is not None
        assert pto.state == "2026-06-03"
        assert pto.attributes["device_class"] == "date"

        # Nine years of lock, so the end follows from PTO alone.
        lock = hass.states.get(_entity_id(hass, entry, "lock_end"))
        assert lock is not None
        assert lock.state == "2035-06-02"

    @pytest.mark.usefixtures("enable_custom_integrations")
    async def test_missing_pto_leaves_both_dates_unknown(self, hass: HomeAssistant) -> None:
        """A system awaiting Permission To Operate has neither date to show."""
        profile = profile_payload(
            AccountProfile(
                (
                    AccountEpoch(
                        effective=date(1970, 1, 1),
                        config=Config(tariff="E-ELEC", pto_date=None),
                    ),
                )
            )
        )
        entry = _entry(profile=profile)
        await _setup_entry(hass, entry)

        pto = hass.states.get(_entity_id(hass, entry, "pto_date"))
        lock = hass.states.get(_entity_id(hass, entry, "lock_end"))
        assert pto is not None and pto.state == "unknown"
        assert lock is not None and lock.state == "unknown"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_cca_validation_loads_the_rate_card_off_the_event_loop(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CCA step must not read its rate card on the event loop.

    `load_rate_card` scandirs the vendored provider directory and parses TOML.
    Called directly from the flow it tripped Home Assistant's blocking-call
    detector -- three warnings per submission, each telling the owner to open a
    bug report -- while every functional assertion still passed, so only a real
    Home Assistant run caught it. Thread identity is the check because that is
    the property that broke: the same call, off the loop, is fine.
    """
    from custom_components.tariffkit import config_flow

    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = config_flow.load_rate_card

    def recording(*args: object, **kwargs: object) -> object:
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(config_flow, "load_rate_card", recording)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "profile_name": "off-loop-cca",
            "supplier": "cca",
            "tariff": "E-ELEC",
            "export_enabled": False,
        },
    )
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

    assert result["type"] == "create_entry"
    assert seen, "the CCA step never loaded a rate card"
    assert loop_thread not in seen, "the rate card was loaded on the event loop"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_reject_a_schedule_the_cca_card_does_not_cover(
    hass: HomeAssistant,
) -> None:
    """The options flow used to accept this and leave the entry unloadable.

    Only the config flow validated the pick against the rate card, so the same
    submission was rejected in-form during setup and written to the entry
    through Configure -- where the reload then failed and every TariffKit
    entity went unavailable with nothing but a log line to say why.
    """
    entry = _entry(profile=_profile_data("E-ELEC"), version=CONFIG_VERSION)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"supplier": "cca", "tariff": "E-1", "export_enabled": False},
    )
    assert result["step_id"] == "settings_delivery"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "base_services_charge_tier": 3,
            "baseline_code": "basic",
            "baseline_territory": "",
        },
    )
    assert result["step_id"] == "settings_cca"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"cca_rate_card": "MCE", "cca_option": "light_green", "cca_pcia_vintage": 2026},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_config"}
    assert "E-1" in result["description_placeholders"]["detail"]
