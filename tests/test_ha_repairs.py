"""The repair an existing entry needs, because setup cannot reach it."""

from __future__ import annotations

import pytest
from custom_components.tariffkit.const import DOMAIN
from custom_components.tariffkit.repairs import ISSUE_NO_METERS, issue_id
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from tests.test_ha_energy import _entry, _meter_options, _setup


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_an_entry_with_no_meters_raises_a_fixable_issue(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)

    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, issue_id(entry.entry_id))
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.translation_key == ISSUE_NO_METERS


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_an_entry_with_meters_raises_nothing(hass: HomeAssistant) -> None:
    entry = _entry(_meter_options())
    await _setup(hass, entry)

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_fix_flow_names_the_counters_and_clears_the_issue(
    hass: HomeAssistant,
) -> None:
    """The point of a *fixable* issue: it repairs in place.

    Telling somebody running totals exist and leaving them to find a menu under
    Configure is the same gap written down.
    """
    from custom_components.tariffkit.repairs import async_create_fix_flow
    from homeassistant.helpers import entity_registry as er

    entry = _entry()
    await _setup(hass, entry)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id(entry.entry_id)) is not None

    flow = await async_create_fix_flow(hass, issue_id(entry.entry_id), {"entry_id": entry.entry_id})
    flow.hass = hass

    form = await flow.async_step_init()
    assert form["step_id"] == "meters"
    assert {str(key) for key in form["data_schema"].schema} >= {
        "grid_import_entity",
        "grid_export_entity",
    }

    result = await flow.async_step_meters(
        {
            "grid_import_entity": "sensor.grid_import_total",
            "grid_export_entity": "sensor.grid_export_total",
            "billing_cycle_start_day": 15,
        }
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    # Written where the coordinator reads it...
    assert entry.options["grid_import_entity"] == "sensor.grid_import_total"
    assert entry.options["billing_cycle_start_day"] == 15
    # ...the issue is gone, cleared by the reload rather than by hand...
    assert registry.async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None
    # ...and the entities the repair exists to create now exist.
    entities = er.async_get(hass)
    assert entities.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_amount_due_cycle")


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
async def test_the_fix_flow_refuses_a_configuration_that_cannot_work(
    hass: HomeAssistant,
) -> None:
    """The same validation as the form under Configure, not a looser one."""
    from custom_components.tariffkit.repairs import async_create_fix_flow

    entry = _entry()
    await _setup(hass, entry)
    flow = await async_create_fix_flow(hass, issue_id(entry.entry_id), {"entry_id": entry.entry_id})
    flow.hass = hass

    result = await flow.async_step_meters(
        {
            "grid_import_entity": "sensor.one_counter",
            "grid_export_entity": "sensor.one_counter",
        }
    )
    assert result["errors"] == {"base": "invalid_meters"}
    assert "both directions" in result["description_placeholders"]["detail"]
