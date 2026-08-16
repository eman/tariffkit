"""Sanitized Home Assistant diagnostics for TariffKit."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from tariffkit.timeutil import now_pacific

from .const import CONF_FORECAST_HOURS, CONF_PREDBAT_ENABLED
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
        "options": {
            CONF_FORECAST_HOURS: coordinator.forecast_hours,
            CONF_PREDBAT_ENABLED: coordinator.predbat_enabled,
        },
    }
