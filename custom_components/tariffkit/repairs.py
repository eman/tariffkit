"""Fixing the one thing an existing entry cannot be told any other way.

Naming the grid counters became part of setting up, but setup runs once: an
entry created before that step existed never sees it, and the form is under
Configure where nobody looks unless they already know it is there. So the
integration says so itself, and fixes it in place.

Deliberately a *fixable* issue rather than a notification. Telling somebody that
running totals are available and leaving them to find a menu is the same gap
written down.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import TariffKitConfigEntry

#: One issue per entry, so a second account is asked about separately.
ISSUE_NO_METERS = "no_meters"


def issue_id(entry_id: str) -> str:
    return f"{ISSUE_NO_METERS}_{entry_id}"


def async_review_meters(hass: HomeAssistant, entry: TariffKitConfigEntry) -> None:
    """Raise or clear the issue, from what the entry actually has.

    Called on every setup, so naming the counters clears it without the user
    having to also dismiss anything, and clearing them raises it again.
    """
    identifier = issue_id(entry.entry_id)
    if entry.runtime_data.meters.configured:
        ir.async_delete_issue(hass, DOMAIN, identifier)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        identifier,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NO_METERS,
        translation_placeholders={"account": entry.title},
        data={"entry_id": entry.entry_id},
    )


class MetersRepairFlow(RepairsFlow):
    """Ask for the counters and save them, rather than pointing at a menu."""

    def __init__(self, entry: TariffKitConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_meters()

    async def async_step_meters(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        from .config_flow import (
            _async_meter_problem,
            _meter_defaults,
            _meter_values,
            _meters_schema,
        )

        if user_input is not None:
            problem = await _async_meter_problem(self.hass, user_input)
            if problem:
                return self.async_show_form(
                    step_id="meters",
                    data_schema=_meters_schema(_meter_defaults(user_input, None)),
                    errors={"base": "invalid_meters"},
                    description_placeholders={"detail": problem},
                )
            self.hass.config_entries.async_update_entry(
                self.entry,
                options={**self.entry.options, **_meter_values(user_input)},
            )
            # The update listener reloads the entry, which re-runs the review
            # above and clears the issue. Deleting it here as well would race
            # that reload for no benefit.
            return self.async_create_entry(data={})

        values = {**self.entry.data, **self.entry.options}
        return self.async_show_form(
            step_id="meters",
            data_schema=_meters_schema(_meter_defaults(values, self.entry.runtime_data.profile)),
            description_placeholders={"detail": ""},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Hand back the flow for one issue, or a bare one if the entry is gone."""
    entry_id = str((data or {}).get("entry_id") or "")
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        # The entry was removed while the issue was open. Returning a flow that
        # immediately finishes is better than raising: the issue is stale, and a
        # traceback in the repairs panel is not a fix.
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        raise ValueError(f"config entry {entry_id} is gone")
    return MetersRepairFlow(entry)
