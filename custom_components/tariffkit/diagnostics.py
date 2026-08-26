"""Sanitized Home Assistant diagnostics for TariffKit."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from tariffkit.timeutil import now_pacific

from .const import CONF_CYCLE_START_DAY, CONF_FORECAST_HOURS, CONF_PREDBAT_ENABLED
from .coordinator import TariffKitConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TariffKitConfigEntry
) -> dict[str, Any]:
    """Return provenance and quality without account evidence or identifiers."""
    coordinator = entry.runtime_data
    data = coordinator.data
    provenance = {
        key: data.provenance[key]
        for key in ("utility", "tariff", "supplier", "tariff_effective", "export_vintage")
        if key in data.provenance
    }
    meters = coordinator.meters
    usage = data.usage
    return {
        "schema_version": entry.version,
        "loaded": True,
        "forecast_hours": coordinator.forecast_hours,
        "predbat_enabled": coordinator.predbat_enabled,
        "active": {
            "start": data.point.start.isoformat(),
            "tariff": data.provenance.get("tariff"),
            "supplier": data.provenance.get("supplier"),
        },
        "quality": data.quality.to_dict(),
        "predbat_warning": data.predbat_warning,
        "provenance": provenance,
        "forecast": {
            "start": data.forecast[0].start.isoformat(),
            "end": data.forecast[-1].end.isoformat(),
            "generated_at": data.generated_at.isoformat(),
        },
        "timezone": hass.config.time_zone,
        "local_date": now_pacific().date().isoformat(),
        # Whether each direction is configured, never which entity it names.
        # An entity id is a meter-source mapping like any other, and this file's
        # contract is that none of them travel in a diagnostics download.
        "meters": {
            "grid_import_configured": meters.import_entity is not None,
            "grid_export_configured": meters.export_entity is not None,
            CONF_CYCLE_START_DAY: meters.cycle_start_day,
        },
        # The bills themselves, not just whether they exist: a running total
        # that looks wrong is almost always a readings problem, and the hour
        # count says how much of the cycle the recorder actually had.
        "usage": None
        if usage is None
        else {
            "hours": len(usage.metered.readings),
            "missing_statistics": len(usage.metered.missing),
            "cycle": usage.cycle.to_dict() if usage.cycle is not None else None,
            "through_yesterday": (
                usage.through_yesterday.to_dict() if usage.through_yesterday is not None else None
            ),
        },
        "options": {
            CONF_FORECAST_HOURS: coordinator.forecast_hours,
            CONF_PREDBAT_ENABLED: coordinator.predbat_enabled,
        },
    }
