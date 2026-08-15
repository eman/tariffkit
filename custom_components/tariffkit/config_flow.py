"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from tariffkit.config import VINTAGE_BY_YEAR
from tariffkit.errors import TariffKitError

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
from .coordinator import config_from_entry

INTERCONNECTION_YEARS = [str(year) for year in sorted(VINTAGE_BY_YEAR)]


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SUPPLIER, default=defaults.get(CONF_SUPPLIER, "bundled")): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["bundled", "cca"], translation_key="supplier"
                    )
                )
            ),
            vol.Required(
                CONF_INTERCONNECTION_YEAR,
                default=str(defaults.get(CONF_INTERCONNECTION_YEAR, 2026)),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=INTERCONNECTION_YEARS)
            ),
            vol.Required(CONF_PTO_DATE, default=defaults.get(CONF_PTO_DATE, "")): (
                selector.DateSelector()
            ),
            vol.Required(
                CONF_ACC_PLUS_SEGMENT,
                default=defaults.get(CONF_ACC_PLUS_SEGMENT, "residential"),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["residential", "residential_low_income", "none"],
                    translation_key="acc_plus_segment",
                )
            ),
            vol.Required(CONF_DISCOUNT, default=defaults.get(CONF_DISCOUNT, "none")): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["none", "care", "fera"], translation_key="discount"
                    )
                )
            ),
            vol.Required(CONF_BSC_TIER, default=defaults.get(CONF_BSC_TIER, 3)): (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=3, step=1, mode="box")
                )
            ),
            vol.Optional(CONF_CCA_NAME, default=defaults.get(CONF_CCA_NAME, "")): str,
            vol.Optional(
                CONF_CCA_PCIA_VINTAGE,
                description={"suggested_value": defaults.get(CONF_CCA_PCIA_VINTAGE)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=2009, max=2030, step=1, mode="box")
            ),
            vol.Optional(
                CONF_CCA_FRANCHISE_FEE,
                description={"suggested_value": defaults.get(CONF_CCA_FRANCHISE_FEE)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=1, step=0.00001, mode="box")
            ),
            vol.Optional(
                CONF_CCA_EXPORT_RATE,
                description={"suggested_value": defaults.get(CONF_CCA_EXPORT_RATE)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=5, step=0.00001, mode="box")
            ),
            vol.Required(
                CONF_FORECAST_HOURS,
                default=defaults.get(CONF_FORECAST_HOURS, DEFAULT_FORECAST_HOURS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=168, step=1, mode="box")
            ),
        }
    )


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_INTERCONNECTION_YEAR] = int(data[CONF_INTERCONNECTION_YEAR])
    data[CONF_BSC_TIER] = int(data[CONF_BSC_TIER])
    data[CONF_FORECAST_HOURS] = int(data[CONF_FORECAST_HOURS])
    if data.get(CONF_CCA_PCIA_VINTAGE) is not None:
        data[CONF_CCA_PCIA_VINTAGE] = int(data[CONF_CCA_PCIA_VINTAGE])
    return data


def _validate(data: dict[str, Any]) -> dict[str, str]:
    """Surface library-level config errors in the form rather than at runtime."""
    try:
        config_from_entry(data)
    except TariffKitError as err:
        return {"base": "invalid_config", "detail": str(err)}
    return {}


class TariffKitConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            errors = _validate(data)
            if not errors:
                await self.async_set_unique_id(
                    f"{data[CONF_SUPPLIER]}-{data[CONF_INTERCONNECTION_YEAR]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="PG&E Rates", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors={k: v for k, v in errors.items() if k == "base"},
            description_placeholders={"detail": errors.get("detail", "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return TariffKitOptionsFlow()


class TariffKitOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            errors = _validate(data)
            if not errors:
                return self.async_create_entry(title="", data=data)

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(defaults),
            errors={k: v for k, v in errors.items() if k == "base"},
            description_placeholders={"detail": errors.get("detail", "")},
        )
