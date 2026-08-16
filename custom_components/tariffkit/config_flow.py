"""Config and options flows for TariffKit account profiles."""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from tariffkit.account import AccountEpoch, AccountError, AccountProfile
from tariffkit.cca import available_rate_cards
from tariffkit.config import VINTAGE_BY_YEAR, Config
from tariffkit.errors import TariffKitError
from tariffkit.timeutil import now_pacific

from .const import (
    CONF_ACC_PLUS_SEGMENT,
    CONF_ACTION,
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
    CONF_EFFECTIVE,
    CONF_EXPORT_ENABLED,
    CONF_FORECAST_HOURS,
    CONF_INTERCONNECTION_YEAR,
    CONF_NSC_RATE,
    CONF_PREDBAT_ENABLED,
    CONF_PROFILE,
    CONF_PROFILE_NAME,
    CONF_PTO_DATE,
    CONF_SUPPLIER,
    CONF_TARIFF,
    CONF_VINTAGE,
    DEFAULT_FORECAST_HOURS,
    DEFAULT_PREDBAT_ENABLED,
    DOMAIN,
)
from .coordinator import config_from_entry
from .profile import (
    LEGACY_EFFECTIVE,
    config_defaults,
    profile_from_entry,
    profile_json,
    profile_payload,
)

INTERCONNECTION_YEARS = [str(year) for year in sorted(VINTAGE_BY_YEAR)]
CCA_OPTIONS = ["light_green", "deep_green"]


def _select(options: list[str], translation_key: str | None = None) -> selector.SelectSelector:
    if translation_key is None:
        return selector.SelectSelector(selector.SelectSelectorConfig(options=options))
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, translation_key=translation_key)
    )


def _profile_schema(defaults: dict[str, Any], *, include_name: bool = True) -> vol.Schema:
    """Choose the account identity and the two branches of manual setup."""
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_SUPPLIER,
            default=defaults.get(CONF_SUPPLIER, "bundled"),
        ): _select(["bundled", "cca"], "supplier"),
        vol.Required(
            CONF_TARIFF,
            default=defaults.get(CONF_TARIFF, "E-ELEC"),
        ): _select(["E-ELEC", "E-TOU-C", "EV2-A"]),
        vol.Required(
            CONF_EXPORT_ENABLED,
            default=defaults.get(CONF_EXPORT_ENABLED, True),
        ): selector.BooleanSelector(),
    }
    if include_name:
        fields = {
            vol.Required(
                CONF_PROFILE_NAME,
                default=defaults.get(CONF_PROFILE_NAME, ""),
            ): selector.TextSelector(),
            **fields,
        }
    return vol.Schema(fields)


def _delivery_schema(
    defaults: dict[str, Any],
    *,
    tariff: str,
    export_enabled: bool,
    effective: bool = False,
) -> vol.Schema:
    """Collect only delivery/NBT fields applicable to this manual branch."""
    fields: dict[Any, Any] = {}
    if effective:
        fields[vol.Required(CONF_EFFECTIVE, default=defaults.get(CONF_EFFECTIVE, ""))] = (
            selector.DateSelector()
        )
    if export_enabled:
        fields[
            vol.Required(
                CONF_INTERCONNECTION_YEAR,
                default=str(defaults.get(CONF_INTERCONNECTION_YEAR) or INTERCONNECTION_YEARS[-1]),
            )
        ] = _select(INTERCONNECTION_YEARS)
        pto_date = defaults.get(CONF_PTO_DATE)
        pto_key = (
            vol.Optional(CONF_PTO_DATE, default=pto_date)
            if pto_date
            else vol.Optional(CONF_PTO_DATE)
        )
        fields[pto_key] = selector.DateSelector()
        fields[
            vol.Required(
                CONF_ACC_PLUS_SEGMENT,
                default=defaults.get(CONF_ACC_PLUS_SEGMENT, "residential"),
            )
        ] = _select(
            ["residential", "residential_low_income", "none"],
            "acc_plus_segment",
        )
        fields[
            vol.Required(
                CONF_DISCOUNT,
                default=defaults.get(CONF_DISCOUNT, "none"),
            )
        ] = _select(["none", "care", "fera"], "discount")
    fields[vol.Required(CONF_BSC_TIER, default=defaults.get(CONF_BSC_TIER, 3))] = (
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=3, step=1, mode=selector.NumberSelectorMode.BOX
            )
        )
    )
    if tariff == "E-TOU-C":
        fields[
            vol.Optional(
                CONF_BASELINE_TERRITORY,
                default=defaults.get(CONF_BASELINE_TERRITORY, ""),
            )
        ] = str
        fields[
            vol.Required(
                CONF_BASELINE_CODE,
                default=defaults.get(CONF_BASELINE_CODE, "basic"),
            )
        ] = _select(["basic", "all_electric"], "baseline_code")
    return vol.Schema(fields)


def _cca_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Select a vendored CCA card rather than entering component rates."""
    return vol.Schema(
        {
            vol.Required(
                CONF_CCA_RATE_CARD,
                default=defaults.get(CONF_CCA_RATE_CARD) or available_rate_cards()[0],
            ): _select(list(available_rate_cards())),
            vol.Required(
                CONF_CCA_OPTION,
                default=defaults.get(CONF_CCA_OPTION, CCA_OPTIONS[0]),
            ): _select(CCA_OPTIONS),
            vol.Required(
                CONF_CCA_PCIA_VINTAGE,
                description={"suggested_value": defaults.get(CONF_CCA_PCIA_VINTAGE)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2009, max=2030, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _history_schema(defaults: dict[str, Any], *, effective: bool = False) -> vol.Schema:
    """Edit an existing snapshot while retaining advanced imported values."""
    fields: dict[Any, Any] = {}
    if effective:
        fields[vol.Required(CONF_EFFECTIVE, default=defaults.get(CONF_EFFECTIVE, ""))] = (
            selector.DateSelector()
        )
    fields[vol.Required(CONF_SUPPLIER, default=defaults.get(CONF_SUPPLIER, "bundled"))] = _select(
        ["bundled", "cca"], "supplier"
    )
    fields[vol.Required(CONF_TARIFF, default=defaults.get(CONF_TARIFF, "E-ELEC"))] = _select(
        ["E-ELEC", "E-TOU-C", "EV2-A"]
    )
    if defaults.get(CONF_INTERCONNECTION_YEAR) is not None:
        fields[
            vol.Required(
                CONF_INTERCONNECTION_YEAR,
                default=str(defaults[CONF_INTERCONNECTION_YEAR]),
            )
        ] = _select(INTERCONNECTION_YEARS)
    else:
        fields[vol.Optional(CONF_INTERCONNECTION_YEAR, default="")] = selector.TextSelector()
    pto_date = defaults.get(CONF_PTO_DATE)
    pto_key = (
        vol.Optional(CONF_PTO_DATE, default=pto_date) if pto_date else vol.Optional(CONF_PTO_DATE)
    )
    fields[pto_key] = selector.DateSelector()
    fields[vol.Optional(CONF_VINTAGE, default=defaults.get(CONF_VINTAGE) or "")] = str
    fields[
        vol.Required(
            CONF_ACC_PLUS_SEGMENT,
            default=defaults.get(CONF_ACC_PLUS_SEGMENT, "residential"),
        )
    ] = _select(["residential", "residential_low_income", "none"], "acc_plus_segment")
    fields[vol.Required(CONF_DISCOUNT, default=defaults.get(CONF_DISCOUNT, "none"))] = _select(
        ["none", "care", "fera"], "discount"
    )
    fields[vol.Required(CONF_BSC_TIER, default=defaults.get(CONF_BSC_TIER, 3))] = (
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=3, step=1, mode=selector.NumberSelectorMode.BOX
            )
        )
    )
    if defaults.get(CONF_TARIFF, "E-ELEC") == "E-TOU-C":
        fields[
            vol.Optional(
                CONF_BASELINE_TERRITORY,
                default=defaults.get(CONF_BASELINE_TERRITORY, ""),
            )
        ] = str
        fields[
            vol.Required(
                CONF_BASELINE_CODE,
                default=defaults.get(CONF_BASELINE_CODE, "basic"),
            )
        ] = _select(["basic", "all_electric"], "baseline_code")
    if defaults.get(CONF_SUPPLIER, "bundled") == "cca":
        fields.update(
            {
                vol.Required(
                    CONF_CCA_RATE_CARD,
                    default=defaults.get(CONF_CCA_RATE_CARD) or available_rate_cards()[0],
                ): _select(list(available_rate_cards())),
                vol.Required(
                    CONF_CCA_OPTION,
                    default=defaults.get(CONF_CCA_OPTION, CCA_OPTIONS[0]),
                ): _select(CCA_OPTIONS),
                vol.Required(
                    CONF_CCA_PCIA_VINTAGE,
                    description={"suggested_value": defaults.get(CONF_CCA_PCIA_VINTAGE)},
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2009, max=2030, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        # Imported profiles may intentionally carry advanced overrides. Keep
        # those fields editable only when they already exist in the snapshot.
        optional_cca = (
            (CONF_CCA_NAME, str),
            (
                CONF_CCA_PCIA_RATE,
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-1, max=1, step=0.00001, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            ),
            (
                CONF_CCA_FRANCHISE_FEE,
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1, step=0.00001, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            ),
            (
                CONF_CCA_EXPORT_RATE,
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=5, step=0.00001, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            ),
            (CONF_CCA_GENERATION_RATES, selector.ObjectSelector()),
        )
        for key, field_type in optional_cca:
            if defaults.get(key) not in (None, "", {}):
                fields[vol.Optional(key, description={"suggested_value": defaults[key]})] = (
                    field_type
                )
    if defaults.get(CONF_NSC_RATE) is not None:
        fields[
            vol.Optional(
                CONF_NSC_RATE,
                description={"suggested_value": defaults.get(CONF_NSC_RATE)},
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=5, step=0.00001, mode=selector.NumberSelectorMode.BOX
            )
        )
    return vol.Schema(fields)


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_FORECAST_HOURS,
                default=int(defaults.get(CONF_FORECAST_HOURS, DEFAULT_FORECAST_HOURS)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_PREDBAT_ENABLED,
                default=bool(defaults.get(CONF_PREDBAT_ENABLED, DEFAULT_PREDBAT_ENABLED)),
            ): selector.BooleanSelector(),
        }
    )


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    year = data.get(CONF_INTERCONNECTION_YEAR)
    if year in (None, ""):
        data[CONF_INTERCONNECTION_YEAR] = None
    else:
        with suppress(TypeError, ValueError):
            data[CONF_INTERCONNECTION_YEAR] = int(year)
    if CONF_BSC_TIER in data:
        data[CONF_BSC_TIER] = int(data[CONF_BSC_TIER])
    if CONF_FORECAST_HOURS in data:
        data[CONF_FORECAST_HOURS] = int(data[CONF_FORECAST_HOURS])
    if CONF_PREDBAT_ENABLED in data:
        data[CONF_PREDBAT_ENABLED] = bool(data[CONF_PREDBAT_ENABLED])
    if data.get(CONF_PTO_DATE) in (None, ""):
        data[CONF_PTO_DATE] = None
    if data.get(CONF_VINTAGE) in (None, ""):
        data[CONF_VINTAGE] = None
    if data.get(CONF_CCA_PCIA_VINTAGE) is not None:
        data[CONF_CCA_PCIA_VINTAGE] = int(data[CONF_CCA_PCIA_VINTAGE])
    return data


def _effective(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise AccountError("effective must be an ISO date") from exc
    raise AccountError("effective must be an ISO date")


def _profile_with_config(
    profile: AccountProfile,
    effective: date,
    config: Config,
    *,
    name: str | None = None,
) -> AccountProfile:
    epochs = list(profile.epochs)
    replacement = AccountEpoch(effective, config)
    for index, epoch in enumerate(epochs):
        if epoch.effective == effective:
            epochs[index] = replacement
            break
    else:
        epochs.append(replacement)
    return AccountProfile(
        epochs=tuple(sorted(epochs, key=lambda epoch: epoch.effective)),
        name=profile.name if name is None else name,
        observations=profile.observations,
        meter_sources=profile.meter_sources,
    )


def _profile_without_epoch(profile: AccountProfile, effective: date) -> AccountProfile:
    epochs = tuple(epoch for epoch in profile.epochs if epoch.effective != effective)
    if len(epochs) == len(profile.epochs):
        raise AccountError("account epoch does not exist")
    if not epochs:
        raise AccountError("an account profile needs at least one epoch")
    return AccountProfile(
        epochs=epochs,
        name=profile.name,
        observations=profile.observations,
        meter_sources=profile.meter_sources,
    )


def _validate(data: dict[str, Any]) -> dict[str, str]:
    """Surface library-level config errors in the form rather than at runtime."""
    try:
        config_from_entry(data)
    except (TariffKitError, TypeError, ValueError) as err:
        return {"base": "invalid_config", "detail": str(err)}
    return {}


def _profile_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace(" ", "-")


def _entry_title(profile: AccountProfile) -> str:
    if profile.name:
        return profile.name
    config = profile.epochs[-1].config
    return f"{config.supplier.value}:{config.tariff}"


def _has_export_config(config: Config) -> bool:
    return (
        config.interconnection_year is not None
        or config.pto_date is not None
        or config.vintage not in (None, "NBT00")
    )


def _manual_config_data(
    identity: dict[str, Any],
    delivery: dict[str, Any],
    cca: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {**identity, **delivery, **(cca or {})}
    if data.get(CONF_EXPORT_ENABLED, True):
        data.setdefault(CONF_INTERCONNECTION_YEAR, int(INTERCONNECTION_YEARS[-1]))
    else:
        # Import-only setup has no NBT choice to collect. NBT00 is the
        # provider's floating default and keeps the Config contract complete.
        data[CONF_INTERCONNECTION_YEAR] = None
        data[CONF_VINTAGE] = "NBT00"
        data[CONF_PTO_DATE] = None
        data.setdefault(CONF_ACC_PLUS_SEGMENT, "residential")
        data.setdefault(CONF_DISCOUNT, "none")
    data.setdefault(CONF_BSC_TIER, 3)
    data.setdefault(CONF_BASELINE_CODE, "basic")
    data.setdefault(CONF_BASELINE_TERRITORY, None)
    if data.get(CONF_SUPPLIER) == "cca":
        data.setdefault(CONF_CCA_NAME, data.get(CONF_CCA_RATE_CARD, ""))
        data.setdefault(CONF_CCA_PCIA_RATE, None)
        data.setdefault(CONF_CCA_FRANCHISE_FEE, None)
        data.setdefault(CONF_CCA_GENERATION_RATES, {})
        data.setdefault(CONF_CCA_EXPORT_RATE, None)
    return _normalize(data)


class TariffKitConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 3

    async def _create_profile(self, data: dict[str, Any]) -> ConfigFlowResult:
        config = config_from_entry(data)
        name = _profile_name(data.get(CONF_PROFILE_NAME, ""))
        if not name:
            raise AccountError("profile name is required")
        profile = AccountProfile(
            epochs=(AccountEpoch(LEGACY_EFFECTIVE, config),),
            name=name,
        )
        await self.async_set_unique_id(f"profile:{name}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=_entry_title(profile),
            data={CONF_PROFILE: profile_payload(profile)},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            action = user_input.get(CONF_ACTION)
            if action == "manual":
                return await self.async_step_manual()
            if action == "import":
                return await self.async_step_import()
        return self.async_show_menu(step_id="user", menu_options=["manual", "import"])

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._manual_identity = dict(user_input)
            if user_input.get(CONF_SUPPLIER) == "cca":
                return await self.async_step_manual_delivery()
            return await self.async_step_manual_delivery()
        return self.async_show_form(
            step_id="manual",
            data_schema=_profile_schema({}),
        )

    async def async_step_manual_delivery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        identity = getattr(self, "_manual_identity", {})
        if user_input is not None:
            self._manual_delivery = dict(user_input)
            if identity.get(CONF_SUPPLIER) == "cca":
                return await self.async_step_manual_cca()
            data = _manual_config_data(identity, self._manual_delivery)
            errors = _validate(data)
            if errors:
                return self.async_show_form(
                    step_id="manual_delivery",
                    data_schema=_delivery_schema(
                        data,
                        tariff=identity.get(CONF_TARIFF, "E-ELEC"),
                        export_enabled=bool(identity.get(CONF_EXPORT_ENABLED, True)),
                    ),
                    errors={"base": errors["base"]},
                    description_placeholders={"detail": errors["detail"]},
                )
            try:
                return await self._create_profile(
                    {**data, CONF_PROFILE_NAME: identity.get(CONF_PROFILE_NAME, "")}
                )
            except (AccountError, TariffKitError) as err:
                return self.async_show_form(
                    step_id="manual_delivery",
                    data_schema=_delivery_schema(
                        data,
                        tariff=identity.get(CONF_TARIFF, "E-ELEC"),
                        export_enabled=bool(identity.get(CONF_EXPORT_ENABLED, True)),
                    ),
                    errors={"base": "invalid_config"},
                    description_placeholders={"detail": str(err)},
                )
        return self.async_show_form(
            step_id="manual_delivery",
            data_schema=_delivery_schema(
                {},
                tariff=identity.get(CONF_TARIFF, "E-ELEC"),
                export_enabled=bool(identity.get(CONF_EXPORT_ENABLED, True)),
            ),
        )

    async def async_step_manual_cca(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        identity = getattr(self, "_manual_identity", {})
        delivery = getattr(self, "_manual_delivery", {})
        if user_input is not None:
            data = _manual_config_data(identity, delivery, dict(user_input))
            errors = _validate(data)
            if errors:
                return self.async_show_form(
                    step_id="manual_cca",
                    data_schema=_cca_schema(user_input),
                    errors={"base": errors["base"]},
                    description_placeholders={"detail": errors["detail"]},
                )
            try:
                return await self._create_profile(
                    {**data, CONF_PROFILE_NAME: identity.get(CONF_PROFILE_NAME, "")}
                )
            except (AccountError, TariffKitError) as err:
                return self.async_show_form(
                    step_id="manual_cca",
                    data_schema=_cca_schema(user_input),
                    errors={"base": "invalid_config"},
                    description_placeholders={"detail": str(err)},
                )
        return self.async_show_form(step_id="manual_cca", data_schema=_cca_schema({}))

    async def async_step_import(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                raw = user_input["profile_json"]
                if not isinstance(raw, str):
                    raise AccountError("profile export must be JSON text")
                imported = AccountProfile.from_json(raw)
                if not imported.name:
                    raise AccountError("imported profile must have a name")
                imported = AccountProfile(
                    epochs=imported.epochs,
                    name=imported.name,
                    observations=imported.observations,
                    meter_sources=imported.meter_sources,
                )
                await self.async_set_unique_id(f"profile:{imported.name}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(imported),
                    data={CONF_PROFILE: profile_payload(imported)},
                )
            except (AccountError, json.JSONDecodeError, TypeError, ValueError) as err:
                errors = {"base": "invalid_profile", "detail": str(err)}
        return self.async_show_form(
            step_id="import",
            data_schema=vol.Schema({vol.Required("profile_json"): selector.TextSelector()}),
            errors={"base": errors["base"]} if "base" in errors else {},
            description_placeholders={"detail": errors.get("detail", "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return TariffKitOptionsFlow()


class TariffKitOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            action = user_input.get("action")
            if action == "settings":
                return await self.async_step_settings()
            if action == "forecast":
                return await self.async_step_forecast()
            if action == "history":
                return await self.async_step_history()
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "forecast", "history"],
        )

    def _values(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _profile(self) -> AccountProfile:
        return profile_from_entry(self._values())

    def _save_profile(self, profile: AccountProfile, **values: Any) -> ConfigFlowResult:
        # OptionsFlow replaces options wholesale; start with the existing
        # options so changing account history does not discard forecast or
        # Predbat settings.
        options = {**self.config_entry.options, **values}
        options[CONF_PROFILE] = profile_payload(profile)
        return self.async_create_entry(title="", data=options)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profile = self._profile()
        try:
            config = profile.config_at(now_pacific())
        except AccountError:
            return self.async_abort(reason="invalid_profile")
        if user_input is not None:
            self._settings_identity = dict(user_input)
            return await self.async_step_settings_delivery()
        return self.async_show_form(
            step_id="settings",
            data_schema=_profile_schema(
                {
                    CONF_SUPPLIER: config.supplier.value,
                    CONF_TARIFF: config.tariff,
                    CONF_EXPORT_ENABLED: _has_export_config(config),
                },
                include_name=False,
            ),
        )

    async def async_step_settings_delivery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        identity = getattr(self, "_settings_identity", {})
        profile = self._profile()
        try:
            current_config = profile.config_at(now_pacific())
        except AccountError:
            return self.async_abort(reason="invalid_profile")
        defaults = config_defaults(current_config)
        if user_input is not None:
            self._settings_delivery = dict(user_input)
            if identity.get(CONF_SUPPLIER) == "cca":
                return await self.async_step_settings_cca()
            data = _manual_config_data(identity, self._settings_delivery)
            try:
                config = config_from_entry(data)
                name = _profile_name(identity.get(CONF_PROFILE_NAME, profile.name))
                today = now_pacific().date()
                effective = max(
                    epoch.effective for epoch in profile.epochs if epoch.effective <= today
                )
                return self._save_profile(
                    _profile_with_config(profile, effective, config, name=name)
                )
            except (AccountError, TariffKitError) as err:
                return self.async_show_form(
                    step_id="settings_delivery",
                    data_schema=_delivery_schema(
                        {**defaults, **user_input},
                        tariff=identity.get(CONF_TARIFF, current_config.tariff),
                        export_enabled=bool(identity.get(CONF_EXPORT_ENABLED, True)),
                    ),
                    errors={"base": "invalid_config"},
                    description_placeholders={"detail": str(err)},
                )
        return self.async_show_form(
            step_id="settings_delivery",
            data_schema=_delivery_schema(
                defaults,
                tariff=identity.get(CONF_TARIFF, current_config.tariff),
                export_enabled=bool(identity.get(CONF_EXPORT_ENABLED, True)),
            ),
        )

    async def async_step_settings_cca(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        identity = getattr(self, "_settings_identity", {})
        delivery = getattr(self, "_settings_delivery", {})
        profile = self._profile()
        try:
            defaults = config_defaults(profile.config_at(now_pacific()))
        except AccountError:
            return self.async_abort(reason="invalid_profile")
        if user_input is not None:
            data = _manual_config_data(identity, delivery, dict(user_input))
            try:
                config = config_from_entry(data)
                name = _profile_name(identity.get(CONF_PROFILE_NAME, profile.name))
                today = now_pacific().date()
                effective = max(
                    epoch.effective for epoch in profile.epochs if epoch.effective <= today
                )
                return self._save_profile(
                    _profile_with_config(profile, effective, config, name=name)
                )
            except (AccountError, TariffKitError) as err:
                return self.async_show_form(
                    step_id="settings_cca",
                    data_schema=_cca_schema({**defaults, **user_input}),
                    errors={"base": "invalid_config"},
                    description_placeholders={"detail": str(err)},
                )
        return self.async_show_form(step_id="settings_cca", data_schema=_cca_schema(defaults))

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            values = _normalize(user_input)
            return self._save_profile(self._profile(), **values)
        return self.async_show_form(
            step_id="forecast",
            data_schema=_options_schema(self._values()),
        )

    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            action = user_input.get("action")
            if action == "inspect":
                return await self.async_step_inspect()
            if action == "add_epoch":
                return await self.async_step_add_epoch()
            if action == "edit_epoch":
                return await self.async_step_edit_epoch()
            if action == "remove_epoch":
                return await self.async_step_remove_epoch()
            if action == "import":
                return await self.async_step_import()
            if action == "export":
                return await self.async_step_export()
        return self.async_show_menu(
            step_id="history",
            menu_options=[
                "inspect",
                "add_epoch",
                "edit_epoch",
                "remove_epoch",
                "import",
                "export",
            ],
        )

    async def async_step_inspect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_history()
        profile = self._profile()
        history = "\n".join(
            f"{epoch.effective.isoformat()}: {epoch.config.tariff}" for epoch in profile.epochs
        )
        return self.async_show_form(
            step_id="inspect",
            data_schema=vol.Schema({}),
            description_placeholders={"history": history},
        )

    async def async_step_add_epoch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profile = self._profile()
        today = now_pacific().date()
        try:
            defaults = config_defaults(profile.config_at(today))
        except AccountError:
            return self.async_abort(reason="invalid_profile")
        defaults[CONF_EFFECTIVE] = today.isoformat()
        if user_input is not None:
            try:
                data = _normalize(user_input)
                errors = _validate(data)
                if not errors:
                    profile = _profile_with_config(
                        profile,
                        _effective(data[CONF_EFFECTIVE]),
                        config_from_entry(data),
                    )
                    return self._save_profile(profile)
            except (AccountError, TariffKitError) as err:
                errors = {"base": "invalid_config", "detail": str(err)}
        return self.async_show_form(
            step_id="add_epoch",
            data_schema=_history_schema(defaults, effective=True),
            errors={"base": errors["base"]} if "base" in errors else {},
            description_placeholders={"detail": errors.get("detail", "")},
        )

    async def async_step_edit_epoch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profile = self._profile()
        if user_input is not None:
            self._editing_effective = _effective(user_input[CONF_EFFECTIVE])
            return await self.async_step_edit_values()
        return self.async_show_form(
            step_id="edit_epoch",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EFFECTIVE): _select(
                        [epoch.effective.isoformat() for epoch in profile.epochs]
                    )
                }
            ),
        )

    async def async_step_edit_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        effective = self._editing_effective
        profile = self._profile()
        epoch = next((item for item in profile.epochs if item.effective == effective), None)
        if epoch is None:
            return self.async_abort(reason="invalid_profile")
        errors: dict[str, str] = {}
        defaults = config_defaults(epoch.config)
        if user_input is not None:
            data = _normalize(user_input)
            errors = _validate(data)
            if not errors:
                try:
                    return self._save_profile(
                        _profile_with_config(profile, effective, config_from_entry(data))
                    )
                except (AccountError, TariffKitError) as err:
                    errors = {"base": "invalid_config", "detail": str(err)}
        return self.async_show_form(
            step_id="edit_values",
            data_schema=_history_schema(defaults),
            errors={"base": errors["base"]} if "base" in errors else {},
            description_placeholders={"detail": errors.get("detail", "")},
        )

    async def async_step_remove_epoch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profile = self._profile()
        if user_input is not None:
            self._removing_effective = _effective(user_input[CONF_EFFECTIVE])
            return await self.async_step_remove_epoch_confirm()
        return self.async_show_form(
            step_id="remove_epoch",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EFFECTIVE): _select(
                        [epoch.effective.isoformat() for epoch in profile.epochs]
                    )
                }
            ),
        )

    async def async_step_remove_epoch_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if not user_input.get("confirm"):
                return await self.async_step_history()
            try:
                return self._save_profile(
                    _profile_without_epoch(self._profile(), self._removing_effective)
                )
            except AccountError:
                return self.async_abort(reason="invalid_profile")
        return self.async_show_form(
            step_id="remove_epoch_confirm",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )

    async def async_step_import(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                raw = user_input["profile_json"]
                if not isinstance(raw, str):
                    raise AccountError("profile export must be JSON text")
                imported = AccountProfile.from_json(raw)
                if imported.name != self._profile().name:
                    raise AccountError("imported profile name must match this config entry")
                return self._save_profile(imported)
            except (AccountError, json.JSONDecodeError, TypeError, ValueError) as err:
                errors = {"base": "invalid_profile", "detail": str(err)}
        return self.async_show_form(
            step_id="import",
            data_schema=vol.Schema({vol.Required("profile_json"): selector.TextSelector()}),
            errors={"base": errors["base"]} if "base" in errors else {},
            description_placeholders={"detail": errors.get("detail", "")},
        )

    async def async_step_export(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_history()
        return self.async_show_form(
            step_id="export",
            data_schema=vol.Schema({}),
            description_placeholders={"profile_json": profile_json(self._profile())},
        )
