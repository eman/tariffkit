"""Coordinator data and refresh logic for the Home Assistant integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from tariffkit.account import AccountProfile, AccountRateEngine
from tariffkit.components import ComponentGroup
from tariffkit.config import CcaConfig, Config
from tariffkit.errors import TariffKitError
from tariffkit.interop import predbat_payload
from tariffkit.interop.predbat import PredbatPayload
from tariffkit.models import PricePoint, Supplier
from tariffkit.timeutil import now_pacific

from .const import (
    CONF_ACC_PLUS_SEGMENT,
    CONF_BASELINE_CODE,
    CONF_BASELINE_TERRITORY,
    CONF_BSC_TIER,
    CONF_CCA_EXPORT_RATE,
    CONF_CCA_FRANCHISE_FEE,
    CONF_CCA_GENERATION_RATES,
    CONF_CCA_NAME,
    CONF_CCA_OPTION,
    CONF_CCA_PCIA_RATE,
    CONF_CCA_PCIA_VINTAGE,
    CONF_CCA_RATE_CARD,
    CONF_DISCOUNT,
    CONF_FORECAST_HOURS,
    CONF_INTERCONNECTION_YEAR,
    CONF_MEDICAL_BASELINE,
    CONF_MEDICAL_KWH_PER_DAY,
    CONF_NSC_RATE,
    CONF_PREDBAT_ENABLED,
    CONF_PROFILE,
    CONF_PTO_DATE,
    CONF_SUPPLIER,
    CONF_TARIFF,
    CONF_VINTAGE,
    DEFAULT_FORECAST_HOURS,
    DEFAULT_PREDBAT_ENABLED,
    DOMAIN,
)
from .profile import profile_from_entry

_LOGGER = logging.getLogger(__name__)

JSONValue = str | int | float | bool | None
Provenance = dict[str, object]


@dataclass(frozen=True, slots=True)
class TariffKitQuality:
    """Quality guardrails that consumers must preserve across a rate horizon."""

    complete: bool
    exact: bool
    locked: bool

    @classmethod
    def from_point(cls, point: PricePoint) -> TariffKitQuality:
        return cls(
            complete=point.import_price.complete and point.export_price.complete,
            exact=point.export_price.exact,
            locked=point.export_price.locked,
        )

    @classmethod
    def from_points(cls, points: tuple[PricePoint, ...]) -> TariffKitQuality:
        if not points:
            raise ValueError("cannot aggregate quality for an empty rate horizon")
        qualities = tuple(cls.from_point(point) for point in points)
        return cls(
            complete=all(quality.complete for quality in qualities),
            exact=all(quality.exact for quality in qualities),
            locked=all(quality.locked for quality in qualities),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "complete": self.complete,
            "exact": self.exact,
            "locked": self.locked,
        }


def _group_values(grouped: dict[ComponentGroup, float]) -> dict[str, float]:
    """Group enum keys as plain strings, so the mapping is JSON-serializable."""
    return {str(group): value for group, value in grouped.items()}


@dataclass(frozen=True, slots=True)
class TariffKitForecastPoint:
    """The compact representation used by the chart entity.

    Carries the grouped component breakdown rather than the tariff's own
    per-line one: five or six numbers an hour that a stacked chart can draw
    directly, instead of the fifteen-odd lines behind them. The per-line detail
    stays on the current-hour price entities, where there is one hour of it
    rather than a whole horizon.
    """

    start: datetime
    end: datetime
    import_price: float
    export_price: float
    spread: float
    import_components: dict[str, float]
    export_components: dict[str, float]

    @classmethod
    def from_point(cls, point: PricePoint) -> TariffKitForecastPoint:
        return cls(
            start=point.start,
            end=point.end,
            import_price=point.import_price.total,
            export_price=point.export_price.total,
            spread=round(point.spread, 6),
            import_components=_group_values(point.import_price.grouped()),
            export_components=_group_values(point.export_price.grouped()),
        )

    def to_dict(self) -> dict[str, JSONValue | dict[str, float]]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "import": self.import_price,
            "export": self.export_price,
            "spread": self.spread,
            "import_components": dict(self.import_components),
            "export_components": dict(self.export_components),
        }


@dataclass(frozen=True, slots=True)
class TariffKitData:
    """All state needed by entities, diagnostics, and response actions."""

    point: PricePoint
    forecast: tuple[TariffKitForecastPoint, ...]
    quality: TariffKitQuality
    provenance: Provenance
    #: Base Services Charge in USD/day. Kept beside the per-kWh prices rather
    #: than inside them: it is a fixed daily amount, so adding it to a marginal
    #: price would misprice every kWh.
    daily_fixed_charge: float
    generated_at: datetime = field(compare=False)
    predbat: PredbatPayload | None = None
    predbat_warning: str | None = None

    def chart_attributes(self) -> dict[str, object]:
        return {
            "rates": [rate.to_dict() for rate in self.forecast],
            "quality": self.quality.to_dict(),
            "generated_at": self.generated_at.isoformat(),
        }


def config_from_entry(data: dict[str, Any]) -> Config:
    """Translate a legacy flat config entry into a library Config."""
    if CONF_PROFILE in data:
        return profile_from_entry(data).config_at(now_pacific())
    supplier = Supplier(data.get(CONF_SUPPLIER, "bundled"))
    cca = None
    if supplier is Supplier.CCA:
        cca = CcaConfig(
            name=data.get(CONF_CCA_NAME, ""),
            rate_card=data.get(CONF_CCA_RATE_CARD) or None,
            option=data.get(CONF_CCA_OPTION, "light_green"),
            pcia_vintage=data.get(CONF_CCA_PCIA_VINTAGE),
            pcia_rate=data.get(CONF_CCA_PCIA_RATE),
            franchise_fee_surcharge=data.get(CONF_CCA_FRANCHISE_FEE),
            generation_rates=data.get(CONF_CCA_GENERATION_RATES, {}),
            export_generation_rate=data.get(CONF_CCA_EXPORT_RATE),
        )
    raw_year = data.get(CONF_INTERCONNECTION_YEAR)
    if raw_year in (None, ""):
        interconnection_year = None
    else:
        try:
            interconnection_year = int(raw_year)
        except (TypeError, ValueError) as err:
            raise TariffKitError("interconnection_year must be a year") from err
    raw_pto = data.get(CONF_PTO_DATE)
    if raw_pto in (None, ""):
        pto = None
    elif isinstance(raw_pto, str):
        try:
            pto = date.fromisoformat(raw_pto)
        except ValueError as err:
            raise TariffKitError("pto_date must be an ISO date") from err
    else:
        pto = raw_pto
    return Config(
        supplier=supplier,
        tariff=data.get(CONF_TARIFF, "E-ELEC"),
        interconnection_year=interconnection_year,
        pto_date=pto,
        vintage=data.get(CONF_VINTAGE) or None,
        acc_plus_segment=data.get(CONF_ACC_PLUS_SEGMENT, "residential"),
        discount=data.get(CONF_DISCOUNT, "none"),
        base_services_charge_tier=data.get(CONF_BSC_TIER, 3),
        baseline_territory=data.get(CONF_BASELINE_TERRITORY) or None,
        baseline_code=data.get(CONF_BASELINE_CODE, "basic"),
        medical_baseline=bool(data.get(CONF_MEDICAL_BASELINE, False)),
        medical_kwh_per_day=(
            float(data[CONF_MEDICAL_KWH_PER_DAY])
            if data.get(CONF_MEDICAL_KWH_PER_DAY) is not None
            else None
        ),
        cca=cca,
        nsc_rate=data.get(CONF_NSC_RATE),
    )


def device_model(info: Mapping[str, Any]) -> str:
    """The device's rate identity: tariff, export vintage, and any CCA."""
    tariff = info.get("tariff")
    model = tariff if isinstance(tariff, str) and tariff else "unknown"
    vintage = info.get("export_vintage")
    if vintage:
        model = f"{model} / {vintage}"
    # On a CCA account the utility still delivers, so it stays the manufacturer
    # and the generation supplier joins the rate identity here.
    generation = _generation_supplier(info)
    if generation:
        model = f"{model} · {generation}"
    return model


def _generation_supplier(info: Mapping[str, Any]) -> str | None:
    """Name the CCA supplying generation, with its product tier when known.

    Returns None for bundled accounts, where PG&E supplies generation too and
    naming it again on the model line would say nothing.
    """
    if str(info.get("supplier")) != str(Supplier.CCA):
        return None
    name = info.get("cca_name") or info.get("cca_rate_card")
    if not isinstance(name, str) or not name:
        return None
    name = name if info.get("cca_name") else name.upper()
    option = info.get("cca_option")
    if isinstance(option, str) and option:
        return f"{name} {option.replace('_', ' ').title()}"
    return name


class TariffKitCoordinator(DataUpdateCoordinator[TariffKitData]):
    """Recompute current and forecast rates at a short boundary-safe interval."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self._device_model: str | None = None
        self.profile: AccountProfile = profile_from_entry({**entry.data, **entry.options})
        self.engine = AccountRateEngine(self.profile)
        self.forecast_hours = int(
            entry.options.get(
                CONF_FORECAST_HOURS,
                entry.data.get(CONF_FORECAST_HOURS, DEFAULT_FORECAST_HOURS),
            )
        )
        if not 1 <= self.forecast_hours <= 168:
            raise TariffKitError("forecast_hours must be between 1 and 168")
        self.predbat_enabled = bool(
            entry.options.get(
                CONF_PREDBAT_ENABLED,
                entry.data.get(CONF_PREDBAT_ENABLED, DEFAULT_PREDBAT_ENABLED),
            )
        )
        self._predbat_key: tuple[date, date] | None = None
        self._predbat: PredbatPayload | None = None
        self._predbat_warning: str | None = None
        if self.predbat_enabled and hass.config.time_zone != "America/Los_Angeles":
            self._predbat_warning = (
                "Predbat expects Home Assistant's time zone to be America/Los_Angeles"
            )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=1),
            always_update=False,
            config_entry=entry,
        )

    async def _async_update_data(self) -> TariffKitData:
        try:
            data = await self.hass.async_add_executor_job(self._compute)
        except TariffKitError as err:
            raise UpdateFailed(str(err)) from err
        self._sync_device_identity(data.provenance)
        return data

    def _sync_device_identity(self, provenance: Provenance) -> None:
        """Keep the device's rate identity current as the profile changes epoch.

        DeviceInfo is read once, when the platform registers the entities, so an
        account that crosses into an epoch on a different tariff, export vintage
        or generation supplier would otherwise keep showing the old identity
        until the entry is reloaded.
        """
        model = device_model(provenance)
        if model == self._device_model:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.entry.entry_id)})
        if device is not None:
            registry.async_update_device(device.id, model=model)
        self._device_model = model

    def _predbat_for(self, moment: datetime) -> PredbatPayload | None:
        if not self.predbat_enabled:
            return None
        active_effective = max(
            effective for effective in self.profile.effective_dates if effective <= moment.date()
        )
        key = (moment.date(), active_effective)
        if key != self._predbat_key:
            self._predbat = predbat_payload(self.engine, moment)
            self._predbat_key = key
        return self._predbat

    def _compute(self) -> TariffKitData:
        point = self.engine.price_now()
        curve = self.engine.forecast(self.forecast_hours, start=point.start)
        curve_points = tuple(curve.points)
        raw_provenance = self.engine.describe(point.start)
        provenance: Provenance = {
            key: raw_provenance[key]
            for key in (
                "utility",
                "tariff",
                "supplier",
                "cca_name",
                "cca_rate_card",
                "cca_option",
                "tariff_effective",
                "tariff_advice_letter",
                "tariff_source",
                "export_vintage",
                "export_years",
                "acc_plus",
                "pto_date",
                "lock_end",
                "account_profile",
            )
            if key in raw_provenance
        }
        return TariffKitData(
            point=point,
            forecast=tuple(TariffKitForecastPoint.from_point(rate) for rate in curve_points),
            quality=TariffKitQuality.from_points(curve_points),
            provenance=provenance,
            daily_fixed_charge=self.engine.daily_fixed_charge(point.start),
            # Keep generated time out of equality so minute polling does not
            # fan out unchanged entity states and websocket attributes.
            generated_at=now_pacific(),
            predbat=self._predbat_for(point.start),
            predbat_warning=self._predbat_warning,
        )

    @property
    def current_hour(self) -> datetime:
        point: PricePoint = self.data.point
        return point.start


type TariffKitConfigEntry = ConfigEntry[TariffKitCoordinator]
