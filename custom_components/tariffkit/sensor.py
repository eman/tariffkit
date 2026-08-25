"""TariffKit price and forecast entities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from tariffkit.billing import Bill, BillingPeriod
from tariffkit.components import (
    EXPORT_GROUPS,
    IMPORT_GROUPS,
    ComponentGroup,
    split_components,
)
from tariffkit.models import ExportPrice, ImportPrice, TouPeriod, Utility
from tariffkit.timeutil import PACIFIC

from .const import (
    ATTR_BUCKETS,
    ATTR_DESCRIPTION,
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
    device_model,
)
from .energy import MeterSettings

PARALLEL_UPDATES = 0
UNIT = "USD/kWh"
DAILY_UNIT = "USD/day"
PTO_DESCRIPTION = (
    "Permission To Operate: the date PG&E authorized the system to export. It "
    "starts the nine-year export rate lock and selects the NBT vintage. Blank "
    "until the utility issues it, which is why export prices stay unlocked "
    "without one."
)
LOCK_END_DESCRIPTION = (
    "Last day the locked export rate vintage is guaranteed, nine years after "
    "Permission To Operate. Prices beyond it are illustrative."
)
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
    #: Only the running totals set this: Home Assistant needs the reset
    #: boundary to keep statistics for a total that goes back to zero.
    last_reset_fn: Callable[[TariffKitData], datetime | None] | None = None


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
        key="pto_date",
        translation_key="pto_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: _provenance_date(data.provenance, "pto_date"),
        attrs_fn=lambda data: {"description": PTO_DESCRIPTION},
    ),
    TariffKitSensorDescription(
        key="lock_end",
        translation_key="lock_end",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:lock-clock",
        value_fn=lambda data: _provenance_date(data.provenance, "lock_end"),
        attrs_fn=lambda data: {"description": LOCK_END_DESCRIPTION},
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


MONEY_UNIT = "USD"
#: The two spans the running totals cover, and what each one's period is.
SPANS = ("today", "cycle")
COST_DESCRIPTION = (
    "Import charges for the metered energy, statutory per-kWh taxes included "
    "and the fixed daily charge excluded."
)
CREDIT_DESCRIPTION = (
    "What the metered exports earned, as a positive number. Exports before "
    "Permission To Operate earn nothing and are not counted."
)
NET_DESCRIPTION = (
    "Charges minus credits, plus the whole of each day's Base Services Charge: "
    "it is incurred for the day of service, not earned by the hour. Positive "
    "means owed, negative means in credit. Priced by the same engine that "
    "reconciles a printed statement, so it is a running bill rather than a "
    "running multiplication."
)
CYCLE_DESCRIPTION = (
    "Cycle to date. Under Net Billing an export credit carries into the next "
    "cycle and settles at the annual true-up, so this is what the cycle has "
    "earned and owes, not a balance due. The cycle_boundary attribute says "
    "whether the period came from a real statement or from the configured "
    "meter-read day, which only approximates one."
)
IMPORT_DESCRIPTION = (
    "Energy taken from the grid, as the meter recorded it. A statement calls this energy delivered."
)
EXPORT_DESCRIPTION = (
    "Energy sent to the grid, as the meter recorded it. A statement calls this "
    "energy received. Exports before Permission To Operate are metered here but "
    "earn no credit."
)


def _absent(data: TariffKitData, description: str) -> dict[str, Any]:
    """Attributes for an entity that has no reading to report.

    An entity that is configured, present, and silent is the worst outcome
    available: it neither gives a number nor says what stopped it. The
    coordinator records why, so say it here.
    """
    return {
        ATTR_QUALITY: {"complete": False},
        "warnings": [data.usage_note] if data.usage_note else [],
        ATTR_DESCRIPTION: description,
    }


def _bill(data: TariffKitData, span: str) -> Bill | None:
    usage = data.usage
    if usage is None:
        return None
    return usage.today if span == "today" else usage.cycle


def _period(data: TariffKitData, span: str) -> BillingPeriod | None:
    usage = data.usage
    if usage is None:
        return None
    return usage.metered.today if span == "today" else usage.metered.cycle


def _last_reset(data: TariffKitData, span: str) -> datetime | None:
    """Local midnight the span began, which is when the total went back to zero.

    Home Assistant needs this to keep long-term statistics for a total that
    resets. Pacific rather than the instance's own zone, because the tariff's
    day is the billing day and a site running on another clock would otherwise
    reset its total in the middle of a peak period.
    """
    period = _period(data, span)
    if period is None:
        return None
    start = period.start
    return datetime(start.year, start.month, start.day, tzinfo=PACIFIC)


def _money_attrs(span: str, description: str) -> Callable[[TariffKitData], dict[str, Any]]:
    """The bill behind one running total, so a surprising figure is auditable."""

    def attrs(data: TariffKitData) -> dict[str, Any]:
        usage = data.usage
        bill = _bill(data, span)
        if usage is None:
            return _absent(data, description)
        if bill is None:
            # An unexplained `unknown` is the worst of both worlds: it neither
            # gives a number nor says what stopped it.
            return {
                ATTR_QUALITY: {"complete": False},
                "warnings": list(usage.warnings(span)),
                **({"cycle_boundary": usage.metered.cycle_source} if span == "cycle" else {}),
                ATTR_DESCRIPTION: description,
            }
        found: dict[str, Any] = {
            "period_start": bill.period.start.isoformat(),
            "period_end": bill.period.end.isoformat(),
            "days": bill.period.days,
            "imported_kwh": round(usage.metered.imported_kwh, 4)
            if span == "cycle"
            else round(usage.metered.imported_today, 4),
            "exported_kwh": round(usage.metered.exported_kwh, 4)
            if span == "cycle"
            else round(usage.metered.exported_today, 4),
            "energy_charges": round(bill.energy_charges, 4),
            "taxes": round(bill.taxes, 4),
            "export_credits": round(-bill.export_credits, 4),
            "fixed_charges": round(bill.fixed_charges, 4),
            ATTR_BUCKETS: [bucket.to_dict() for bucket in bill.buckets],
            ATTR_QUALITY: {"complete": usage.complete},
            "compensated_kwh": round(bill.exported_kwh, 4),
            "warnings": list(usage.warnings(span)),
            **({"cycle_boundary": usage.metered.cycle_source} if span == "cycle" else {}),
            ATTR_DESCRIPTION: (
                f"{description} {CYCLE_DESCRIPTION}" if span == "cycle" else description
            ),
        }
        return found

    return attrs


def _energy_attrs(span: str, direction: str) -> Callable[[TariffKitData], dict[str, Any]]:
    def attrs(data: TariffKitData) -> dict[str, Any]:
        usage = data.usage
        if usage is None:
            return _absent(
                data,
                IMPORT_DESCRIPTION if direction == "import" else EXPORT_DESCRIPTION,
            )
        period = _period(data, span)
        found: dict[str, Any] = {
            ATTR_DESCRIPTION: IMPORT_DESCRIPTION if direction == "import" else EXPORT_DESCRIPTION,
            "source_entity": usage.metered.source(direction),
        }
        if period is not None:
            found["period_start"] = period.start.isoformat()
            found["period_end"] = period.end.isoformat()
        bill = _bill(data, span)
        if direction == "export" and bill is not None:
            # What the tariff will actually pay for, which is less than the
            # meter saw whenever a site exported before Permission To Operate.
            found["compensated_kwh"] = round(bill.exported_kwh, 4)
        return found

    return attrs


def _reset_at(span: str) -> Callable[[TariffKitData], datetime | None]:
    def reset(data: TariffKitData) -> datetime | None:
        return _last_reset(data, span)

    return reset


def _money_sensor(
    span: str,
    key: str,
    description: str,
    value: Callable[[Bill], float],
) -> TariffKitSensorDescription:
    def state(data: TariffKitData) -> float | None:
        bill = _bill(data, span)
        return None if bill is None else round(value(bill), 4)

    return TariffKitSensorDescription(
        key=f"{key}_{span}",
        translation_key=f"{key}_{span}",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=MONEY_UNIT,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=state,
        attrs_fn=_money_attrs(span, description),
        last_reset_fn=_reset_at(span),
    )


def _energy_sensor(span: str, direction: str) -> TariffKitSensorDescription:
    key = "grid_import" if direction == "import" else "grid_export"

    def state(data: TariffKitData) -> float | None:
        usage = data.usage
        if usage is None:
            return None
        metered = usage.metered
        if direction == "import":
            return round(metered.imported_kwh if span == "cycle" else metered.imported_today, 4)
        return round(metered.exported_kwh if span == "cycle" else metered.exported_today, 4)

    return TariffKitSensorDescription(
        key=f"{key}_{span}",
        translation_key=f"{key}_{span}",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=state,
        attrs_fn=_energy_attrs(span, direction),
        last_reset_fn=_reset_at(span),
    )


BANK_DESCRIPTION = (
    "Export credits earned but not yet spent, carried between billing cycles. "
    "Under Net Billing a credit does not settle at the end of the cycle that "
    "earned it -- it banks, offsets later charges, and survives the annual "
    "true-up, which claws back only what Net Surplus Compensation already paid "
    "for. A balance at the last cycle close, not a figure any single statement "
    "prints. Where a Community Choice Aggregator supplies generation these are "
    "two banks on unrelated settlement calendars, and adding them together "
    "would give a number that never settles as one."
)


def _bank_sensor(holder: str) -> TariffKitSensorDescription:
    """The running credit bank.

    `state_class` is measurement rather than total, and there is deliberately no
    `monetary` device class. A bank is a *stock*, not an accumulator: it rises
    and falls, and Home Assistant would otherwise record each fall as a negative
    contribution to a lifetime sum that means nothing. Home Assistant permits
    only `total` alongside `monetary`, so the device class is the thing that has
    to go; the unit still says what the number is.
    """

    def state(data: TariffKitData) -> float | None:
        if data.bank is None:
            return None
        return round(data.bank.held_by(holder), 4)

    def attrs(data: TariffKitData) -> dict[str, Any]:
        if data.bank is None:
            return {
                ATTR_QUALITY: {"complete": False},
                ATTR_DESCRIPTION: BANK_DESCRIPTION,
                "warnings": ["no closed billing cycle has been priced yet"],
            }
        found = dict(data.bank.to_dict())
        found[ATTR_QUALITY] = {"complete": found.pop("complete")}
        found[ATTR_DESCRIPTION] = BANK_DESCRIPTION
        return found

    key = "export_credit_bank" if holder == "utility" else "export_credit_bank_generation"
    return TariffKitSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=MONEY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=state,
        attrs_fn=attrs,
    )


def usage_sensors(meters: MeterSettings) -> tuple[TariffKitSensorDescription, ...]:
    """The running-total entities the configured meters can actually support.

    An account with no export entity gets no export-credit entity rather than a
    permanent zero: unlike the component bands, which stay put so a chart never
    has to be reconfigured, a credit that is structurally absent is not a series
    with nothing in it -- it is a question the meters cannot answer.
    """
    if not meters.configured:
        return ()
    found: list[TariffKitSensorDescription] = []
    for span in SPANS:
        if meters.import_entity:
            found.append(_energy_sensor(span, "import"))
        if meters.export_entity:
            found.append(_energy_sensor(span, "export"))
        if meters.import_entity:
            found.append(
                _money_sensor(
                    span,
                    "energy_cost",
                    COST_DESCRIPTION,
                    lambda b: b.energy_charges + b.taxes,
                )
            )
        if meters.export_entity:
            found.append(
                _money_sensor(
                    span,
                    "export_credit",
                    CREDIT_DESCRIPTION,
                    lambda b: -b.export_credits,
                )
            )
        found.append(_money_sensor(span, "net_cost", NET_DESCRIPTION, lambda b: b.total))
    if meters.export_entity:
        # No export meter, no export credit, so no bank to carry. Two entities
        # because a Community Choice Aggregator account has two banks settling on
        # unrelated calendars; on a bundled account the generation one reports
        # the same single bank, and its name says whose it is.
        found.append(_bank_sensor("utility"))
        found.append(_bank_sensor("generation"))
    return tuple(found)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TariffKitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: TariffKitCoordinator = entry.runtime_data
    descriptions = (*SENSORS, *usage_sensors(coordinator.meters))
    _prune_removed(hass, entry, descriptions)
    async_add_entities(
        TariffKitSensor(coordinator, entry, description) for description in descriptions
    )


@callback
def _prune_removed(
    hass: HomeAssistant,
    entry: TariffKitConfigEntry,
    descriptions: tuple[TariffKitSensorDescription, ...],
) -> None:
    """Drop registry entries for entities this configuration no longer creates.

    The usage entities exist only while the meters that answer them are
    configured, so clearing an export counter -- or all of them -- shrinks the
    set. Nothing removes a registry entry on its own, so without this the
    entities that went away linger forever as `unavailable`, and the only cure
    is deleting each by hand. Reload runs this before adding, so narrowing the
    configuration cleans up in the same pass that applies it.
    """
    registry = er.async_get(hass)
    wanted = {f"{entry.entry_id}_{description.key}" for description in descriptions}
    for existing in er.async_entries_for_config_entry(registry, entry.entry_id):
        if existing.unique_id not in wanted:
            registry.async_remove(existing.entity_id)


def _provenance_date(info: Mapping[str, Any], key: str) -> date | None:
    """Read an ISO date out of provenance, tolerating absence.

    Both dates are optional: pto_date is blank until the utility issues it, and
    lock_end cannot be derived without it.
    """
    raw = info.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


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
            # The running totals' time-of-use breakdown, for the same reason as
            # the forecast curve: it is rewritten every minute on six entities,
            # and the state that a history graph actually draws is the total.
            ATTR_BUCKETS,
            # Fixed explanatory prose. The recorder hashes the whole attribute
            # dict, so the numbers beside it changing every minute means this
            # text is re-stored every minute too -- several hundred bytes per
            # entity per tick, for a string that never differs.
            ATTR_DESCRIPTION,
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
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name=name,
            manufacturer=manufacturer,
            model=device_model(info),
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

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.last_reset_fn is None:
            return None
        return self.entity_description.last_reset_fn(self.coordinator.data)
