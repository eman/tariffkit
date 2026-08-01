"""User-facing configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigError
from .models import Supplier

AccPlusSegment = Literal["residential", "residential_low_income", "none"]
Discount = Literal["none", "care", "fera"]

#: Interconnection-application year -> NBT vintage. Systems that do not qualify
#: for a nine-year lock use the floating vintage, NBT00.
VINTAGE_BY_YEAR = {
    2023: "NBT23",
    2024: "NBT24",
    2025: "NBT25",
    2026: "NBT26",
}
FLOATING_VINTAGE = "NBT00"

LOCK_YEARS = 9
_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class CcaConfig:
    """Settings for customers whose generation comes from a CCA or ESP.

    PG&E still delivers, so the delivery half of both the import price and the
    export credit continues to come from PG&E's tariff. Generation comes from
    the CCA, and this package ships no CCA rate data -- supply it here.
    """

    name: str = ""
    #: Vendored rate card to price generation from, e.g. ``"mce"``. Without one,
    #: supply ``generation_rates`` directly.
    rate_card: str | None = None
    #: Product tier on that rate card, e.g. MCE's light_green / deep_green.
    option: str = "light_green"
    #: Vintage year for the PCIA the customer pays in place of the bundled PCIA.
    pcia_vintage: int | None = None
    #: PCIA in $/kWh, taken straight from a bill. Overrides ``pcia_vintage``,
    #: which only covers the vintages published on the E-ELEC sheet.
    pcia_rate: float | None = None
    #: Schedule E-FFS franchise fee surcharge, $/kWh. Not published on the
    #: E-ELEC sheet, so it must be supplied rather than guessed.
    franchise_fee_surcharge: float | None = None
    #: CCA generation charge by season and TOU period, e.g.
    #: ``{"summer": {"peak": 0.123, ...}, "winter": {...}}``.
    generation_rates: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Per-kWh export compensation the CCA pays, if any. Under NBT the PG&E
    #: file's generation component does not apply to CCA customers.
    export_generation_rate: float | None = None

    @property
    def complete(self) -> bool:
        has_generation = bool(self.generation_rates) or self.rate_card is not None
        return has_generation and self.franchise_fee_surcharge is not None


@dataclass(frozen=True, slots=True)
class Config:
    """Everything needed to price a kWh for one service agreement."""

    utility: str = "PGE"
    tariff: str = "E-ELEC"
    supplier: Supplier = Supplier.BUNDLED

    #: Calendar year of the completed interconnection application. Selects both
    #: the NBT vintage and the ACC Plus row.
    interconnection_year: int | None = 2026
    #: Permission-To-Operate date. Starts the nine-year rate lock.
    pto_date: date | None = date(2026, 6, 3)
    #: Overrides the vintage derived from ``interconnection_year``.
    vintage: str | None = None

    acc_plus_segment: AccPlusSegment = "residential"
    discount: Discount = "none"
    base_services_charge_tier: Literal[1, 2, 3] = 3

    cca: CcaConfig | None = None

    def __post_init__(self) -> None:
        if self.supplier is Supplier.CCA and self.cca is None:
            raise ConfigError("supplier='cca' requires a CcaConfig")
        if self.vintage is None and self.interconnection_year is None:
            raise ConfigError("set either interconnection_year or vintage")
        if self.discount != "none" and self.acc_plus_segment == "residential":
            # CARE/FERA customers qualify for the much larger low-income ACC
            # Plus adder; silently applying the standard one would understate
            # the export credit by several cents per kWh.
            raise ConfigError(
                f"discount={self.discount!r} implies acc_plus_segment="
                "'residential_low_income'; set it explicitly"
            )

    @property
    def resolved_vintage(self) -> str:
        """The NBT vintage whose matrix applies to this customer."""
        if self.vintage is not None:
            return self.vintage
        assert self.interconnection_year is not None
        return VINTAGE_BY_YEAR.get(self.interconnection_year, FLOATING_VINTAGE)

    @property
    def lock_end(self) -> date | None:
        """Last date covered by the nine-year rate lock, inclusive.

        ``None`` for a floating (NBT00) customer, who has no lock at all.
        """
        if self.pto_date is None or self.resolved_vintage == FLOATING_VINTAGE:
            return None
        return self.pto_date.replace(year=self.pto_date.year + LOCK_YEARS) - _ONE_DAY

    def with_(self, **changes: Any) -> Config:
        return replace(self, **changes)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        data = dict(raw)
        if "cca" in data and isinstance(data["cca"], dict):
            data["cca"] = CcaConfig(**data["cca"])
        if "supplier" in data:
            data["supplier"] = Supplier(data["supplier"])
        for key in ("pto_date",):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = date.fromisoformat(value)
        unknown = set(data) - {f.name for f in cls.__dataclass_fields__.values()}
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def from_toml(cls, path: str | Path) -> Config:
        with Path(path).open("rb") as handle:
            return cls.from_dict(tomllib.load(handle))

    @classmethod
    def from_env(cls, base: Config | None = None) -> Config:
        """Overlay ``NEM_RATES_*`` environment variables onto ``base``."""
        config = base or cls()
        overrides: dict[str, Any] = {}
        if value := os.environ.get("NEM_RATES_SUPPLIER"):
            overrides["supplier"] = Supplier(value)
        if value := os.environ.get("NEM_RATES_VINTAGE"):
            overrides["vintage"] = value
        if value := os.environ.get("NEM_RATES_INTERCONNECTION_YEAR"):
            overrides["interconnection_year"] = int(value)
        if value := os.environ.get("NEM_RATES_PTO_DATE"):
            overrides["pto_date"] = date.fromisoformat(value)
        if value := os.environ.get("NEM_RATES_ACC_PLUS_SEGMENT"):
            overrides["acc_plus_segment"] = value
        if value := os.environ.get("NEM_RATES_DISCOUNT"):
            overrides["discount"] = value
        if value := os.environ.get("NEM_RATES_BSC_TIER"):
            overrides["base_services_charge_tier"] = int(value)
        return replace(config, **overrides) if overrides else config

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load from ``path``, else the default config file, else defaults.

        Environment variables overlay whichever source is used.
        """
        if path is not None:
            return cls.from_env(cls.from_toml(path))
        default = default_config_path()
        if default.is_file():
            return cls.from_env(cls.from_toml(default))
        return cls.from_env()


def default_config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root) if root else Path.home() / ".config"
    return base / "nem-rates" / "config.toml"
