"""Community Choice Aggregation generation rate cards.

A CCA supplies generation while PG&E still delivers, so a CCA customer's import
price is PG&E's delivery stack plus the CCA's own generation rates, a vintaged
PCIA, and a franchise fee surcharge. Only providers with a vendored card here
can be priced end to end; everything else needs rates supplied via CcaConfig.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from .data import versioned
from .errors import ConfigError, DataError
from .models import Utility


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

    @property
    def supported_schedules(self) -> frozenset[str]:
        """PG&E schedule slugs for which this card publishes generation rates."""
        return frozenset(self.raw["generation"])

    def generation(
        self, schedule: str, season: str, period: str, option: str = "light_green"
    ) -> float:
        """Generation charge in $/kWh, including any product premium.

        Keyed by PG&E schedule: a CCA prices each one separately, and the rates
        differ enough that borrowing another schedule's card is a large silent
        error rather than an approximation. MCE winter off-peak is 0.06754 on
        E-ELEC against 0.11042 on E-TOU-C.
        """
        slug = schedule.lower().replace("-", "")
        by_schedule = self.raw["generation"]
        if slug not in by_schedule:
            raise DataError(
                f"{self.provider}: no generation rates for schedule {schedule!r}; "
                f"this card covers {sorted(by_schedule)}"
            )
        try:
            base = float(by_schedule[slug][season][period])
        except KeyError as exc:
            raise DataError(
                f"{self.provider}: no {schedule} generation rate for {season}/{period}"
            ) from exc
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

    @property
    def credits_acc_plus(self) -> bool:
        """Whether this provider credits the ACC Plus adder in its own right.

        The utility credits it too, on its own page and at the same rate, so a
        provider that does this pays the customer a second adder rather than a
        share of one. Default false: crediting an adder nobody pays is an
        invented credit, which is the class of defect the audit exists to
        catch, so a provider claims this only once a statement has shown it.
        """
        return bool(self.raw.get("export", {}).get("credits_acc_plus", False))

    def care_fera_bonus(self, on: date) -> float:
        """Low-income export bonus this provider pays, $/kWh, zero once expired.

        MCE's Solar Billing Plan tariff (effective 2023-12-01): "CARE and FERA
        customers will receive a $0.05/kWh generation export bonus credit on
        all exports until December 31, 2028. This credit is additional to any
        PG&E delivery-based export bonus credits for eligible customers."

        The values were vendored and then read by nothing, so a CARE or FERA
        account on such a provider was short the bonus on every exported kWh --
        a larger per-kWh effect than the ACC Plus adder that is applied.
        """
        export = self.raw.get("export", {})
        rate = float(export.get("care_fera_bonus_rate", 0.0))
        if not rate:
            return 0.0
        through = export.get("care_fera_bonus_through")
        if through is not None and on > date.fromisoformat(str(through)):
            return 0.0
        return rate


@lru_cache(maxsize=8)
def load_rate_card(provider: str, on: date) -> CcaRateCard:
    """The provider's rate card in force on ``on``.

    A CCA reprices on a date like any other supplier -- MCE cut generation by
    about 14% effective 2026-04-01 -- so a card is chosen by date rather than
    "whichever is vendored". Before this took a date at all, a January bill was
    priced with April's card and looked entirely reasonable while being wrong by
    the whole repricing.
    """
    slug = provider.lower().replace(" ", "_")
    relative = f"cca/{slug}"
    try:
        version = versioned.load(relative, on, label=f"CCA {provider}")
    except DataError as exc:
        # Distinguish "we vendor nothing for this CCA" -- where the answer is to
        # supply rates yourself -- from "we vendor it but not back that far",
        # where the answer is to vendor the missing vintage. versioned.load
        # phrases the second; only the first needs rewriting here.
        if "no vendored data at" not in str(exc):
            raise
        raise DataError(
            f"no vendored rate card for CCA {provider!r}; supply rates via "
            f"CcaConfig(generation_rates=..., franchise_fee_surcharge=...)"
        ) from exc
    if version.raw.get("schema") != 1:
        raise DataError(f"{provider}: unsupported rate card schema {version.raw.get('schema')}")
    raw = dict(version.raw)
    try:
        raw["utility"] = Utility(raw["utility"]).value
    except (KeyError, ValueError) as exc:
        raise DataError(f"{provider}: invalid utility identifier") from exc
    return CcaRateCard(provider=str(raw["provider"]), raw=raw)


def available_rate_cards() -> tuple[str, ...]:
    return ("MCE",)
