"""Credential-free account-profile storage for the Home Assistant integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from tariffkit.account import AccountEpoch, AccountProfile
from tariffkit.config import Config
from tariffkit.errors import TariffKitError

from .const import CONF_PROFILE

LEGACY_EFFECTIVE = date(1970, 1, 1)


def sanitize_profile(profile: AccountProfile) -> AccountProfile:
    """Remove the optional keyring-set reference before HA persists a profile."""
    return AccountProfile(
        epochs=profile.epochs,
        name=profile.name,
        credential_set=None,
        observations=profile.observations,
        meter_sources=profile.meter_sources,
    )


def profile_payload(profile: AccountProfile) -> dict[str, object]:
    """Return the same schema as the CLI export, without credential metadata."""
    return sanitize_profile(profile).to_dict()


def profile_json(profile: AccountProfile) -> str:
    """Return canonical JSON suitable for a copy/paste export."""
    return sanitize_profile(profile).to_json()


def profile_from_entry(data: Mapping[str, Any]) -> AccountProfile:
    """Load a profile from entry data, migrating the old flat config shape."""
    raw = data.get(CONF_PROFILE)
    if isinstance(raw, str):
        return sanitize_profile(AccountProfile.from_json(raw))
    if isinstance(raw, Mapping):
        return sanitize_profile(AccountProfile.from_dict(raw))
    if raw is not None:
        raise TariffKitError("Home Assistant profile must be a JSON object or string")

    from .coordinator import config_from_entry

    return AccountProfile(epochs=(AccountEpoch(LEGACY_EFFECTIVE, config_from_entry(dict(data))),))


def config_defaults(config: Config) -> dict[str, Any]:
    """Flatten a library config into the integration's form fields."""
    values = config.to_dict()
    cca = values.pop("cca", None)
    result: dict[str, Any] = dict(values)
    result["supplier"] = config.supplier.value
    if result.get("interconnection_year") is not None:
        result["interconnection_year"] = str(result["interconnection_year"])
    else:
        result["interconnection_year"] = ""
    if config.pto_date is not None:
        result["pto_date"] = config.pto_date.isoformat()
    else:
        result["pto_date"] = ""
    if isinstance(cca, Mapping):
        result.update(
            {
                "cca_name": cca.get("name", ""),
                "cca_rate_card": cca.get("rate_card") or "",
                "cca_option": cca.get("option", "light_green"),
                "cca_pcia_vintage": cca.get("pcia_vintage"),
                "cca_pcia_rate": cca.get("pcia_rate"),
                "cca_franchise_fee_surcharge": cca.get("franchise_fee_surcharge"),
                "cca_export_generation_rate": cca.get("export_generation_rate"),
                "cca_generation_rates": cca.get("generation_rates", {}),
            }
        )
    else:
        result.update(
            {
                "cca_name": "",
                "cca_rate_card": "",
                "cca_option": "light_green",
                "cca_generation_rates": {},
            }
        )
    return result
