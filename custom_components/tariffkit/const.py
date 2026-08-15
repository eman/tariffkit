"""Constants for the tariffkit integration."""

from __future__ import annotations

DOMAIN = "tariffkit"

CONF_SUPPLIER = "supplier"
CONF_INTERCONNECTION_YEAR = "interconnection_year"
CONF_PTO_DATE = "pto_date"
CONF_ACC_PLUS_SEGMENT = "acc_plus_segment"
CONF_BSC_TIER = "base_services_charge_tier"
CONF_DISCOUNT = "discount"
CONF_FORECAST_HOURS = "forecast_hours"

CONF_CCA_NAME = "cca_name"
CONF_CCA_PCIA_VINTAGE = "cca_pcia_vintage"
CONF_CCA_FRANCHISE_FEE = "cca_franchise_fee_surcharge"
CONF_CCA_EXPORT_RATE = "cca_export_generation_rate"

DEFAULT_FORECAST_HOURS = 48

ATTR_FORECAST = "forecast"
#: Predbat reads these two off whichever entity apps.yaml points
#: metric_octopus_import / metric_octopus_export at.
ATTR_RAW_TODAY = "raw_today"
ATTR_RAW_TOMORROW = "raw_tomorrow"
#: EMHASS runtime parameters, ready to drop into a rest_command body. Bare
#: lists, positional against EMHASS's timeline, so the horizon travels with them.
ATTR_LOAD_COST = "load_cost_forecast"
ATTR_PROD_PRICE = "prod_price_forecast"
ATTR_HORIZON = "prediction_horizon"
