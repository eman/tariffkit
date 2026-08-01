"""Community Choice Aggregation generation rate cards.

A CCA supplies generation while PG&E still delivers, so a CCA customer's import
price is PG&E's delivery stack plus the CCA's own generation rates, a vintaged
PCIA, and a franchise fee surcharge. Only providers with a vendored card here
can be priced end to end; everything else needs rates supplied via CcaConfig.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from .data import data_exists, read_data_text
from .errors import ConfigError, DataError


@dataclass(frozen=True, slots=True)
class CcaRateCard:
    """A vendored CCA generation rate card."""

    provider: str
    raw: dict[str, Any]

    @property
    def effective(self) -> date:
        return date.fromisoformat(self.raw["effective"])

    @property
    def source_url(self) -> str:
        return str(self.raw.get("source_url", ""))

    def generation(self, season: str, period: str, option: str = "light_green") -> float:
        """Generation charge in $/kWh, including any product premium."""
        try:
            base = float(self.raw["generation"][season][period])
        except KeyError as exc:
            raise DataError(f"{self.provider}: no generation rate for {season}/{period}") from exc
        premium = self.raw.get("options", {}).get(option)
        if premium is None:
            raise ConfigError(
                f"{self.provider}: unknown product option {option!r}; "
                f"available: {sorted(self.raw.get('options', {}))}"
            )
        return base + float(premium)

    def cost_relief_credit(self, on: date) -> float:
        """A time-limited per-kWh credit, or zero once it lapses.

        Dated rather than folded into the rates so it stops applying on its own
        instead of quietly understating prices in later years.
        """
        credit = self.raw.get("cost_relief_credit")
        if not credit:
            return 0.0
        through = credit.get("through")
        if through and on > date.fromisoformat(through):
            return 0.0
        return float(credit["rate"])

    @property
    def solar_bonus_fraction(self) -> float:
        """Bonus paid as a fraction of the base export credit."""
        return float(self.raw.get("export", {}).get("solar_bonus_fraction", 0.0))

    @property
    def export_credit_verified(self) -> bool:
        """Whether this provider's export credit basis has been confirmed."""
        return bool(self.raw.get("export", {}).get("export_credit_verified", False))


@lru_cache(maxsize=8)
def load_rate_card(provider: str) -> CcaRateCard:
    slug = provider.lower().replace(" ", "_")
    relative = f"cca/{slug}.toml"
    if not data_exists(relative):
        raise DataError(
            f"no vendored rate card for CCA {provider!r}; supply rates via "
            f"CcaConfig(generation_rates=..., franchise_fee_surcharge=...)"
        )
    raw = tomllib.loads(read_data_text(relative))
    if raw.get("schema") != 1:
        raise DataError(f"{provider}: unsupported rate card schema {raw.get('schema')}")
    return CcaRateCard(provider=str(raw["provider"]), raw=raw)


def available_rate_cards() -> tuple[str, ...]:
    return ("MCE",)
