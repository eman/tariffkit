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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from tariffkit.components import (
    EXPORT_GROUPS,
    IMPORT_GROUPS,
    ComponentGroup,
    split_components,
)
from tariffkit.models import ExportPrice, ImportPrice, TouPeriod, Utility

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
DAILY_UNIT = "USD/day"
FIXED_CHARGE_DESCRIPTION = (
    "AB 205 Base Services Charge, billed per day of service. It is not a per-kWh "
    "price, so it does not belong in a stacked price chart and is not part of "
    "Import Price."
)
#: Icons per component group, so a stacked chart's legend is legible in the
#: entity list too.
GROUP_ICONS: dict[ComponentGroup, str] = {
    ComponentGroup.GENERATION: "mdi:factory",
    ComponentGroup.DISTRIBUTION: "mdi:home-lightning-bolt",
    ComponentGroup.TRANSMISSION: "mdi:transmission-tower",
    ComponentGroup.DELIVERY: "mdi:transmission-tower",
    ComponentGroup.SURCHARGES: "mdi:bank",
    ComponentGroup.CREDITS: "mdi:sale",
    ComponentGroup.OTHER: "mdi:dots-horizontal",
}
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
        if direction == "import":
            import_price = data.point.import_price
            quality = TariffKitQuality(
                complete=import_price.complete,
                exact=True,
                locked=True,
            )
            components = import_price.components
        else:
            export_price = data.point.export_price
            quality = TariffKitQuality(
                complete=export_price.complete,
                exact=export_price.exact,
                locked=export_price.locked,
            )
            components = export_price.components
        attrs: dict[str, Any] = {
            "components": dict(components),
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


def _rate_data_status(data: TariffKitData) -> str:
    if not data.quality.complete:
        return "incomplete"
    if not data.quality.exact:
        return "illustrative"
    if not data.quality.locked:
        return "unlocked"
    return "current"


def _rate_data_attrs(data: TariffKitData) -> dict[str, Any]:
    provenance = data.provenance
    return {
        "pto_date": provenance.get("pto_date"),
        "export_rate_lock_end": provenance.get("lock_end"),
        "export_vintage": provenance.get("export_vintage"),
        "tariff_effective": provenance.get("tariff_effective"),
        "tariff_advice_letter": provenance.get("tariff_advice_letter"),
        ATTR_QUALITY: data.quality.to_dict(),
        "source_url": provenance.get("tariff_source"),
    }


@dataclass(frozen=True, kw_only=True)
class TariffKitSensorDescription(SensorEntityDescription):
    """Adds typed value and attribute extractors to a sensor description."""

    value_fn: Callable[[TariffKitData], Any]
    attrs_fn: Callable[[TariffKitData], dict[str, Any]] | None = None


def _price_for(data: TariffKitData, direction: str) -> ImportPrice | ExportPrice:
    return data.point.import_price if direction == "import" else data.point.export_price


def _component_sensor(direction: str, group: ComponentGroup) -> TariffKitSensorDescription:
    """One stackable series: this direction's price, restricted to one group.

    The groups for a direction sum to that direction's price, so charting all of
    them stacked reproduces Import Price or Export Price exactly. The series
    exists whether or not the account pays that kind of charge -- a bundled
    customer's ``credits`` band sits at zero rather than the entity vanishing --
    because a chart configuration should not have to change when a discount or a
    CCA does.
    """
    groups = IMPORT_GROUPS if direction == "import" else EXPORT_GROUPS

    def value(data: TariffKitData) -> float:
        return _price_for(data, direction).grouped()[group]

    def attrs(data: TariffKitData) -> dict[str, Any]:
        price = _price_for(data, direction)
        # A retail schedule is published, not vintaged, so only the export side
        # can be unlocked or inexact -- the import flags are constants.
        quality = (
            TariffKitQuality(complete=price.complete, exact=True, locked=True)
            if isinstance(price, ImportPrice)
            else TariffKitQuality(complete=price.complete, exact=price.exact, locked=price.locked)
        )
        return {
            # The tariff's own lines behind this band, so the roll-up is
            # auditable from the entity rather than only from the source.
            "components": dict(split_components(price.components, groups)[group]),
            "direction": direction,
            ATTR_QUALITY: _quality_attributes(quality),
        }

    return TariffKitSensorDescription(
        key=f"{direction}_{group}",
        translation_key=f"{direction}_{group}",
        native_unit_of_measurement=UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon=GROUP_ICONS[group],
        value_fn=value,
        attrs_fn=attrs,
    )


def _component_sensors() -> tuple[TariffKitSensorDescription, ...]:
    return tuple(
        _component_sensor(direction, group)
        for direction, groups in (("import", IMPORT_GROUPS), ("export", EXPORT_GROUPS))
        for group in groups
    )


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
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chart-timeline-variant",
        value_fn=lambda data: data.forecast[-1].end,
        attrs_fn=_forecast_attrs,
    ),
    TariffKitSensorDescription(
        key="daily_fixed_charge",
        translation_key="daily_fixed_charge",
        native_unit_of_measurement=DAILY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:cash-clock",
        value_fn=lambda data: round(data.daily_fixed_charge, 6),
        attrs_fn=lambda data: {"description": FIXED_CHARGE_DESCRIPTION},
    ),
    TariffKitSensorDescription(
        key="rate_data_status",
        translation_key="rate_data_status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=["current", "unlocked", "illustrative", "incomplete"],
        icon="mdi:database-check",
        value_fn=_rate_data_status,
        attrs_fn=_rate_data_attrs,
    ),
    *_component_sensors(),
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
        configuration_url: str | None = None
        if (
            isinstance(source, str)
            and parsed is not None
            and parsed.scheme in {"http", "https"}
            and parsed.netloc
        ):
            configuration_url = source
        profile_name = self.coordinator.profile.name
        name = f"TariffKit — {profile_name}" if profile_name else "TariffKit Rates"
        utility = info.get("utility")
        manufacturer = Utility(utility).display_name if isinstance(utility, str) else "TariffKit"
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
