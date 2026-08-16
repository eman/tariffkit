"""Coordinator data and refresh logic for the Home Assistant integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from tariffkit.account import AccountProfile, AccountRateEngine
from tariffkit.config import CcaConfig, Config
from tariffkit.errors import TariffKitError
from tariffkit.interop import predbat_payload
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
PredbatPayload = dict[str, dict[str, list[dict[str, Any]]]]


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


@dataclass(frozen=True, slots=True)
class TariffKitForecastPoint:
    """The compact, component-free representation used by the chart entity."""

    start: datetime
    end: datetime
    import_price: float
    export_price: float
    spread: float

    @classmethod
    def from_point(cls, point: PricePoint) -> TariffKitForecastPoint:
        return cls(
            start=point.start,
            end=point.end,
            import_price=point.import_price.total,
            export_price=point.export_price.total,
            spread=round(point.spread, 6),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "import": self.import_price,
            "export": self.export_price,
            "spread": self.spread,
        }


@dataclass(frozen=True, slots=True)
class TariffKitData:
    """All state needed by entities, diagnostics, and response actions."""

    point: PricePoint
    forecast: tuple[TariffKitForecastPoint, ...]
    quality: TariffKitQuality
    provenance: Provenance
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
        cca=cca,
        nsc_rate=data.get(CONF_NSC_RATE),
    )


class TariffKitCoordinator(DataUpdateCoordinator[TariffKitData]):
    """Recompute current and forecast rates at a short boundary-safe interval."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
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
            return await self.hass.async_add_executor_job(self._compute)
        except TariffKitError as err:
            raise UpdateFailed(str(err)) from err

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
                "tariff_effective",
                "tariff_advice_letter",
                "tariff_source",
                "export_vintage",
                "export_years",
                "acc_plus",
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
