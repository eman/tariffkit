"""Constants for the tariffkit integration."""

from __future__ import annotations

DOMAIN = "tariffkit"

CONF_SUPPLIER = "supplier"
CONF_TARIFF = "tariff"
CONF_INTERCONNECTION_YEAR = "interconnection_year"
CONF_PTO_DATE = "pto_date"
CONF_VINTAGE = "vintage"
CONF_ACC_PLUS_SEGMENT = "acc_plus_segment"
CONF_BSC_TIER = "base_services_charge_tier"
CONF_DISCOUNT = "discount"
CONF_BASELINE_TERRITORY = "baseline_territory"
CONF_BASELINE_CODE = "baseline_code"
CONF_MEDICAL_BASELINE = "medical_baseline"
CONF_MEDICAL_KWH_PER_DAY = "medical_kwh_per_day"
CONF_FORECAST_HOURS = "forecast_hours"
CONF_PREDBAT_ENABLED = "predbat_enabled"
CONF_PROFILE_NAME = "profile_name"
CONF_EXPORT_ENABLED = "export_enabled"
CONF_NSC_RATE = "nsc_rate"
CONF_PROFILE = "profile"
CONF_GRID_IMPORT_ENTITY = "grid_import_entity"
CONF_GRID_EXPORT_ENTITY = "grid_export_entity"
CONF_CYCLE_START_DAY = "billing_cycle_start_day"
CONF_EFFECTIVE = "effective"
CONF_ACTION = "action"

CONF_CCA_NAME = "cca_name"
CONF_CCA_RATE_CARD = "cca_rate_card"
CONF_CCA_OPTION = "cca_option"
CONF_CCA_PCIA_VINTAGE = "cca_pcia_vintage"
CONF_CCA_PCIA_RATE = "cca_pcia_rate"
CONF_CCA_FRANCHISE_FEE = "cca_franchise_fee_surcharge"
CONF_CCA_EXPORT_RATE = "cca_export_generation_rate"
CONF_CCA_GENERATION_RATES = "cca_generation_rates"

DEFAULT_FORECAST_HOURS = 48
DEFAULT_PREDBAT_ENABLED = False
#: 0 means "no meter-read day configured", so a cycle runs from the first of
#: the calendar month. A real PG&E cycle is 27 to 33 days and closes on a
#: roughly fixed day of the month, which is what a non-zero value names.
DEFAULT_CYCLE_START_DAY = 0

SERVICE_GET_RATES = "get_rates"
SERVICE_GET_EMHASS_FORECAST = "get_emhass_forecast"
CONF_CONFIG_ENTRY = "config_entry"
CONF_START = "start"
CONF_END = "end"
CONF_DATE = "date"
CONF_HORIZON = "horizon"
CONF_RESOLUTION = "resolution"
SUPPORTED_RESOLUTIONS = (15, 30, 60)

#: The running totals' time-of-use breakdown. Named here because it is
#: excluded from the recorder alongside the other large attributes.
ATTR_BUCKETS = "buckets"
#: Fixed explanatory prose on an entity; excluded from the recorder.
ATTR_DESCRIPTION = "description"
ATTR_RATES = "rates"
ATTR_QUALITY = "quality"
ATTR_GENERATED_AT = "generated_at"
ATTR_PROVENANCE = "provenance"
#: Predbat reads these two off whichever entity apps.yaml points
#: metric_octopus_import / metric_octopus_export at.
ATTR_RAW_TODAY = "raw_today"
ATTR_RAW_TOMORROW = "raw_tomorrow"
#: EMHASS runtime parameters, ready to drop into a rest_command body. Bare
#: lists, positional against EMHASS's timeline, so the horizon travels with them.
ATTR_LOAD_COST = "load_cost_forecast"
ATTR_PROD_PRICE = "prod_price_forecast"
