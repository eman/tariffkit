"""TariffKit rate sensors for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from tariffkit.errors import TariffKitError

from .const import (
    CONF_CYCLE_START_DAY,
    CONF_FORECAST_HOURS,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_PREDBAT_ENABLED,
    CONF_PROFILE,
    DEFAULT_FORECAST_HOURS,
    DEFAULT_PREDBAT_ENABLED,
    DOMAIN,
)
from .coordinator import TariffKitConfigEntry, TariffKitCoordinator
from .profile import profile_from_entry, profile_payload
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_VERSION = 3
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, object]) -> bool:
    """Register response actions even when no TariffKit entry is loaded."""
    await async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: TariffKitConfigEntry) -> bool:
    """Migrate entries into canonical profile data plus options."""
    if entry.version >= CONFIG_VERSION:
        return True
    try:
        # Data is the canonical location after version 2; older options-flow
        # entries may still carry a profile there, so use it only when data
        # does not already contain one.
        merged = {**entry.options, **entry.data}
        profile = profile_from_entry(merged)
        options: dict[str, Any] = {
            CONF_FORECAST_HOURS: int(
                entry.options.get(
                    CONF_FORECAST_HOURS,
                    entry.data.get(CONF_FORECAST_HOURS, DEFAULT_FORECAST_HOURS),
                )
            ),
            CONF_PREDBAT_ENABLED: bool(
                entry.options.get(
                    CONF_PREDBAT_ENABLED,
                    entry.data.get(CONF_PREDBAT_ENABLED, DEFAULT_PREDBAT_ENABLED),
                )
            ),
        }
        # The meter settings travel too. Rebuilding options from a fixed list of
        # keys dropped them, so an entry migrated from version 1 or 2 lost its
        # running-total entities: the account still priced rates, and the
        # metered figures simply stopped existing.
        #
        # Copied only when present, never defaulted. `MeterSettings.from_entry`
        # reads a key that is present and empty as a deliberate "no entity" that
        # suppresses the profile's own `meter_sources.ha` -- so writing `""` for
        # an entry that never had the key would lose the meters a second way,
        # for anyone whose mapping lives on the profile.
        for key in (CONF_GRID_IMPORT_ENTITY, CONF_GRID_EXPORT_ENTITY, CONF_CYCLE_START_DAY):
            if key in entry.options:
                options[key] = entry.options[key]
            elif key in entry.data:
                options[key] = entry.data[key]
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_PROFILE: profile_payload(profile)},
            options=options,
            unique_id=f"profile:{profile.name}" if profile.name else entry.unique_id,
            version=CONFIG_VERSION,
        )
    except (TariffKitError, TypeError, ValueError) as err:
        _LOGGER.error("Unable to migrate TariffKit config entry %s: %s", entry.entry_id, err)
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TariffKitConfigEntry) -> bool:
    coordinator = TariffKitCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TariffKitConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: TariffKitConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
