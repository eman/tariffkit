"""Coordinator that refreshes on the hour boundary."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from nem_rates import CcaConfig, Config, RateEngine, Supplier
from nem_rates.errors import NemRatesError

from .const import (
    CONF_ACC_PLUS_SEGMENT,
    CONF_BSC_TIER,
    CONF_CCA_EXPORT_RATE,
    CONF_CCA_FRANCHISE_FEE,
    CONF_CCA_NAME,
    CONF_CCA_PCIA_VINTAGE,
    CONF_DISCOUNT,
    CONF_FORECAST_HOURS,
    CONF_INTERCONNECTION_YEAR,
    CONF_PTO_DATE,
    CONF_SUPPLIER,
    DEFAULT_FORECAST_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def config_from_entry(data: dict[str, Any]) -> Config:
    """Translate the config entry into a library Config."""
    supplier = Supplier(data.get(CONF_SUPPLIER, "bundled"))
    cca = None
    if supplier is Supplier.CCA:
        cca = CcaConfig(
            name=data.get(CONF_CCA_NAME, ""),
            pcia_vintage=data.get(CONF_CCA_PCIA_VINTAGE),
            franchise_fee_surcharge=data.get(CONF_CCA_FRANCHISE_FEE),
            export_generation_rate=data.get(CONF_CCA_EXPORT_RATE),
        )
    pto = data.get(CONF_PTO_DATE)
    return Config(
        supplier=supplier,
        interconnection_year=data.get(CONF_INTERCONNECTION_YEAR),
        pto_date=date.fromisoformat(pto) if isinstance(pto, str) else pto,
        acc_plus_segment=data.get(CONF_ACC_PLUS_SEGMENT, "residential"),
        discount=data.get(CONF_DISCOUNT, "none"),
        base_services_charge_tier=data.get(CONF_BSC_TIER, 3),
        cca=cca,
    )


class NemRatesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Recomputes prices each hour.

    The underlying data is static and local, so an "update" is pure computation
    -- no network, no rate limit, no failure mode beyond a bad config.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.engine = RateEngine(config_from_entry({**entry.data, **entry.options}))
        self.forecast_hours = (
            entry.options.get(CONF_FORECAST_HOURS)
            or entry.data.get(CONF_FORECAST_HOURS)
            or DEFAULT_FORECAST_HOURS
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Prices change on the hour; a short interval keeps the sensor from
            # lagging the boundary without any meaningful cost.
            update_interval=timedelta(minutes=1),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.hass.async_add_executor_job(self._compute)
        except NemRatesError as err:
            raise UpdateFailed(str(err)) from err

    def _compute(self) -> dict[str, Any]:
        point = self.engine.price_now()
        curve = self.engine.forecast(self.forecast_hours, start=point.start)
        return {
            "point": point,
            "forecast": [
                {
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "import": p.import_price.total,
                    "export": p.export_price.total,
                    "spread": round(p.spread, 6),
                }
                for p in curve
            ],
            "info": self.engine.describe(),
            "daily_fixed_charge": self.engine.daily_fixed_charge(),
        }

    @property
    def current_hour(self) -> datetime:
        return self.data["point"].start
