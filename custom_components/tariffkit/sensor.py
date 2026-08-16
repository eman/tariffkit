"""TariffKit price and forecast entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from tariffkit import TouPeriod

from .const import (
    ATTR_GENERATED_AT,
    ATTR_LOAD_COST,
    ATTR_PROD_PRICE,
    ATTR_PROVENANCE,
    ATTR_QUALITY,
    ATTR_RATES,
    ATTR_RAW_TODAY,
    ATTR_RAW_TOMORROW,
    DOMAIN,
)
from .coordinator import (
    TariffKitConfigEntry,
    TariffKitCoordinator,
    TariffKitData,
    TariffKitQuality,
)

PARALLEL_UPDATES = 0
UNIT = "USD/kWh"
SPREAD_DESCRIPTION = (
    "Export compensation minus avoided import cost; excludes battery efficiency, "
    "degradation, and inverter losses."
)


def _quality_attributes(quality: TariffKitQuality) -> dict[str, bool]:
    return quality.to_dict()


def _price_attrs(direction: str) -> Callable[[TariffKitData], dict[str, Any]]:
    """Return compact attributes for one current price direction."""

    if direction not in {"import", "export"}:
        raise ValueError(f"unsupported price direction {direction!r}")

    def extract(data: TariffKitData) -> dict[str, Any]:
        price = data.point.import_price if direction == "import" else data.point.export_price
        quality = (
            TariffKitQuality(
                complete=price.complete,
                exact=True,
                locked=True,
            )
            if direction == "import"
            else TariffKitQuality(
                complete=price.complete,
                exact=price.exact,
                locked=price.locked,
            )
        )
        attrs: dict[str, Any] = {
            "components": dict(price.components),
            ATTR_QUALITY: _quality_attributes(quality),
            ATTR_PROVENANCE: dict(data.provenance),
        }
        if data.predbat is not None:
            attrs.update(data.predbat[direction])
            if data.predbat_warning is not None:
                attrs["predbat_warning"] = data.predbat_warning
        return attrs

    return extract


def _spread_attrs(data: TariffKitData) -> dict[str, Any]:
    """Attributes for the derived export-minus-import spread."""
    quality = TariffKitQuality.from_point(data.point)
    return {
        ATTR_QUALITY: _quality_attributes(quality),
        ATTR_PROVENANCE: dict(data.provenance),
        "description": SPREAD_DESCRIPTION,
    }


def _forecast_attrs(data: TariffKitData) -> dict[str, Any]:
    return {
        ATTR_RATES: [rate.to_dict() for rate in data.forecast],
        ATTR_QUALITY: data.quality.to_dict(),
        ATTR_GENERATED_AT: data.generated_at.isoformat(),
    }


@dataclass(frozen=True, kw_only=True)
class TariffKitSensorDescription(SensorEntityDescription):
    """Adds typed value and attribute extractors to a sensor description."""

    value_fn: Callable[[TariffKitData], Any]
    attrs_fn: Callable[[TariffKitData], dict[str, Any]] | None = None


SENSORS: tuple[TariffKitSensorDescription, ...] = (
    TariffKitSensorDescription(
        key="import_price",
        translation_key="import_price",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: data.point.import_price.total,
        attrs_fn=_price_attrs("import"),
    ),
    TariffKitSensorDescription(
        key="export_price",
        translation_key="export_price",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:transmission-tower-export",
        value_fn=lambda data: data.point.export_price.total,
        attrs_fn=_price_attrs("export"),
    ),
    TariffKitSensorDescription(
        key="spread",
        translation_key="spread",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:swap-vertical",
        value_fn=lambda data: round(data.point.spread, 6),
        attrs_fn=_spread_attrs,
    ),
    TariffKitSensorDescription(
        key="tou_period",
        translation_key="tou_period",
        device_class=SensorDeviceClass.ENUM,
        options=[str(period) for period in TouPeriod],
        icon="mdi:clock-outline",
        value_fn=lambda data: str(data.point.import_price.period),
        attrs_fn=lambda data: {
            "season": str(data.point.import_price.season),
            ATTR_QUALITY: _quality_attributes(
                TariffKitQuality(
                    complete=data.point.import_price.complete,
                    exact=True,
                    locked=True,
                )
            ),
        },
    ),
    TariffKitSensorDescription(
        key="forecast_through",
        translation_key="forecast_through",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:chart-timeline-variant",
        value_fn=lambda data: data.forecast[-1].end,
        attrs_fn=_forecast_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TariffKitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TariffKitCoordinator = entry.runtime_data
    async_add_entities(TariffKitSensor(coordinator, entry, description) for description in SENSORS)


class TariffKitSensor(CoordinatorEntity[TariffKitCoordinator], SensorEntity):
    """A sensor backed by the shared typed coordinator result."""

    entity_description: TariffKitSensorDescription
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            ATTR_RATES,
            ATTR_RAW_TODAY,
            ATTR_RAW_TOMORROW,
            ATTR_LOAD_COST,
            ATTR_PROD_PRICE,
        }
    )

    def __init__(
        self,
        coordinator: TariffKitCoordinator,
        entry: TariffKitConfigEntry,
        description: TariffKitSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Use the active profile epoch for service-device metadata."""
        info = self.coordinator.data.provenance
        source = info.get("tariff_source")
        parsed = urlparse(source) if isinstance(source, str) else None
        configuration_url = (
            source
            if parsed is not None and parsed.scheme in {"http", "https"} and parsed.netloc
            else None
        )
        profile_name = self.coordinator.profile.name
        name = f"TariffKit — {profile_name}" if profile_name else "TariffKit Rates"
        utility = info.get("utility")
        manufacturer = utility if isinstance(utility, str) and utility else "TariffKit"
        tariff = info.get("tariff")
        model = tariff if isinstance(tariff, str) and tariff else "unknown"
        vintage = info.get("export_vintage")
        if vintage:
            model = f"{model} / {vintage}"
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name=name,
            manufacturer=manufacturer,
            model=model,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=configuration_url,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
