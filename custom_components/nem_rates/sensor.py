"""Sensor entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from nem_rates import TouPeriod

from .const import (
    ATTR_FORECAST,
    ATTR_HORIZON,
    ATTR_LOAD_COST,
    ATTR_PROD_PRICE,
    ATTR_RAW_TODAY,
    ATTR_RAW_TOMORROW,
    DOMAIN,
)
from .coordinator import NemRatesCoordinator

#: Ends in "/kWh", which is the whole of what Home Assistant's Energy dashboard
#: requires of a price entity -- it checks neither device_class nor currency.
UNIT = "USD/kWh"


@dataclass(frozen=True, kw_only=True)
class NemRatesSensorDescription(SensorEntityDescription):
    """Adds the value and attribute extractors to the standard description."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _price_attrs(key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Component breakdown, plus the payloads other energy systems read.

    Note the scale mismatch: ``raw_today``/``raw_tomorrow`` are cents while the
    state and the EMHASS series are dollars. That is deliberate -- Predbat is a
    UK tool whose thresholds assume pence, and it reads only these attributes,
    never the state.
    """
    # Explicit rather than "import if ... else export": a typo'd key would
    # otherwise silently attach the export payloads to the wrong sensor.
    direction = {"import_price": "import", "export_price": "export"}[key]
    emhass_key = ATTR_LOAD_COST if direction == "import" else ATTR_PROD_PRICE

    def extract(data: dict[str, Any]) -> dict[str, Any]:
        price = getattr(data["point"], key)
        return {
            **price.to_dict(),
            ATTR_FORECAST: data["forecast"],
            emhass_key: data["emhass"][emhass_key],
            ATTR_HORIZON: data["emhass"][ATTR_HORIZON],
            **data["predbat"][direction],
        }

    return extract


SENSORS: tuple[NemRatesSensorDescription, ...] = (
    NemRatesSensorDescription(
        key="import_price",
        translation_key="import_price",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:transmission-tower-export",
        value_fn=lambda data: data["point"].import_price.total,
        attrs_fn=_price_attrs("import_price"),
    ),
    NemRatesSensorDescription(
        key="export_price",
        translation_key="export_price",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: data["point"].export_price.total,
        attrs_fn=_price_attrs("export_price"),
    ),
    NemRatesSensorDescription(
        key="spread",
        translation_key="spread",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:swap-vertical",
        value_fn=lambda data: round(data["point"].spread, 6),
        attrs_fn=lambda data: {
            "favours_export": data["point"].spread > 0,
            ATTR_FORECAST: data["forecast"],
        },
    ),
    NemRatesSensorDescription(
        key="tou_period",
        translation_key="tou_period",
        # A fixed set of string states, which is what ENUM is for. No
        # state_class: these are labels, not a series to run statistics over.
        device_class=SensorDeviceClass.ENUM,
        options=[str(period) for period in TouPeriod],
        icon="mdi:clock-outline",
        value_fn=lambda data: str(data["point"].import_price.period),
        attrs_fn=lambda data: {"season": str(data["point"].import_price.season)},
    ),
    NemRatesSensorDescription(
        key="daily_fixed_charge",
        translation_key="daily_fixed_charge",
        native_unit_of_measurement="USD/d",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:cash-clock",
        entity_registry_enabled_default=False,
        value_fn=lambda data: data["daily_fixed_charge"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NemRatesCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NemRatesSensor(coordinator, entry, description) for description in SENSORS)


class NemRatesSensor(CoordinatorEntity[NemRatesCoordinator], SensorEntity):
    entity_description: NemRatesSensorDescription
    _attr_has_entity_name = True
    # The recorder writes every attribute on every state change, and the
    # coordinator ticks each minute. These are large, wholly derivable, and
    # useless as history -- a forecast recorded hourly is just noise.
    #
    # Home Assistant folds this across the MRO in Entity.__init_subclass__, so it
    # has to be a class attribute: setting it per-description or in __init__ does
    # nothing. Listing an attribute an entity does not have is harmless.
    _unrecorded_attributes = frozenset(
        {
            ATTR_FORECAST,
            ATTR_LOAD_COST,
            ATTR_PROD_PRICE,
            ATTR_RAW_TODAY,
            ATTR_RAW_TOMORROW,
            "components",
        }
        # ATTR_HORIZON stays recorded: it is a single small int, and seeing the
        # horizon change over time is genuinely useful when debugging EMHASS.
    )

    def __init__(
        self,
        coordinator: NemRatesCoordinator,
        entry: ConfigEntry,
        description: NemRatesSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        info = coordinator.data["info"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PG&E Rates",
            manufacturer=str(info.get("utility", "PGE")),
            model=f"{info.get('tariff')} / {info.get('export_vintage')}",
            configuration_url=str(info.get("tariff_source") or None),
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
