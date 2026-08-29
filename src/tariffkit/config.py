"""User-facing configuration."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigError
from .models import Supplier, Utility

AccPlusSegment = Literal["residential", "residential_low_income", "none"]
Discount = Literal["none", "care", "fera"]
#: PG&E's Code B (basic) and Code H (all-electric) baseline quantity columns.
BaselineCode = Literal["basic", "all_electric"]

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
#: Which Base Services Charge tier each discount programme is billed on.
#: D-CARE assigns CARE customers to tier 1; FERA takes the middle tier, and an
#: undiscounted account the standard one. Pinned here because nothing in the
#: rate data ties the two fields together and the default is tier 3.
BSC_TIER_BY_DISCOUNT: dict[str, int] = {"none": 3, "care": 1, "fera": 2}
#: The tier every profile serialized before the two fields were connected.
_LEGACY_BSC_TIER = 3
_ONE_DAY = timedelta(days=1)


def _env_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


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

    def __post_init__(self) -> None:
        if self.export_generation_rate is not None and self.rate_card is not None:
            # `export_generation_rate` wins in the export path, and taking that
            # branch skips the card's solar bonus and its ACC Plus adder
            # entirely -- a 22% under-credit into the CCA's bank on MCE, with
            # `complete` still reporting True. Setting both looks like "card for
            # generation charges, explicit rate for exports"; it silently means
            # something else, so it is refused rather than resolved.
            raise ConfigError(
                "set either cca.export_generation_rate or cca.rate_card, not both: "
                "an explicit export rate bypasses the card's solar bonus and ACC Plus"
            )

    @property
    def complete(self) -> bool:
        has_generation = bool(self.generation_rates) or self.rate_card is not None
        # A PCIA vintage supplies both the PCIA and the matching E-FFS value
        # from the vendored tariff sheet.  Requiring the surcharge to be copied
        # into every profile made the otherwise valid ``rate_card + pcia_vintage``
        # form look incomplete, even though RetailTariff can price it exactly.
        has_pcia = self.pcia_rate is not None or self.pcia_vintage is not None
        has_franchise_fee = (
            self.franchise_fee_surcharge is not None or self.pcia_vintage is not None
        )
        return has_generation and has_pcia and has_franchise_fee


@dataclass(frozen=True, slots=True)
class Config:
    """Everything needed to price a kWh for one service agreement."""

    utility: Utility = Utility.PACIFIC_GAS_AND_ELECTRIC
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
    #: Leave unset to take the tier the discount programme implies, which is
    #: what the tariff assigns. Set it only to override that.
    base_services_charge_tier: Literal[1, 2, 3] | None = None

    #: Baseline territory letter, printed on the bill as e.g. "Baseline
    #: Territory X". Only schedules with a baseline allowance use it -- E-TOU-C
    #: among those vendored -- and only when computing a whole bill.
    baseline_territory: str | None = None
    #: "basic" for a gas-heated home, "all_electric" where space heating is
    #: electric. PG&E prints this as Code B or Code H; the bill's "Heat Source"
    #: line says which.
    baseline_code: BaselineCode = "basic"
    #: Medical Baseline enrollment. Tiered schedules receive additional
    #: baseline quantity; E-ELEC, E-TOU-D, and EV2-A receive D-MEDICAL.
    medical_baseline: bool = False
    medical_kwh_per_day: float | None = None

    #: SmartRate is event-driven. Event dates and the date through which the
    #: supplied calendar is authoritative are explicit so a missing future
    #: event cannot be silently priced as an ordinary day.
    smartrate: bool = False
    smartrate_events: tuple[date, ...] = ()
    smartrate_known_through: date | None = None

    cca: CcaConfig | None = None

    #: Net Surplus Compensation rate, $/kWh, for the annual true-up.
    #:
    #: Left unset because for a CCA account nobody publishes one in advance: MCE
    #: determines its Solar Billing Plan rate at cash-out. When this is ``None``
    #: the true-up falls back to PG&E's published series as a stand-in and marks
    #: the result estimated. Set it once a real cash-out statement says what was
    #: actually paid.
    nsc_rate: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "utility", Utility(self.utility))
        # Coerce the string forms of the enums. Supplier is a StrEnum, so a
        # plain "cca" compares equal to Supplier.CCA but is not it -- and every
        # branch that matters tests identity. Constructing Config directly with
        # a string therefore priced a CCA customer as bundled, silently and with
        # entirely plausible numbers. from_dict already coerced; direct
        # construction did not, which is the path library callers take.
        # Written through the value rather than as an isinstance guard: the
        # annotation promises a Supplier, so mypy reads any such guard as dead
        # code. That promise is exactly what is not enforced at runtime -- a
        # caller passing "cca" got a str, which compares equal to Supplier.CCA
        # but is not it, and every branch that matters tests identity.
        # Supplier(Supplier.CCA) is Supplier.CCA, so this is a no-op when the
        # annotation was honoured and a coercion when it was not.
        object.__setattr__(self, "supplier", Supplier(self.supplier))
        if self.supplier is Supplier.CCA and self.cca is None:
            raise ConfigError("supplier='cca' requires a CcaConfig")
        if self.smartrate and self.supplier is not Supplier.BUNDLED:
            raise ConfigError("SmartRate is available only with bundled PG&E generation")
        if self.smartrate_events and not self.smartrate:
            raise ConfigError("smartrate_events requires smartrate=true")
        if self.smartrate and self.smartrate_known_through is None:
            raise ConfigError("smartrate=true requires smartrate_known_through")
        if self.smartrate_known_through is not None and any(
            event > self.smartrate_known_through for event in self.smartrate_events
        ):
            raise ConfigError("a SmartRate event falls after smartrate_known_through")
        if self.medical_kwh_per_day is not None and self.medical_kwh_per_day <= 0:
            raise ConfigError("medical_kwh_per_day must be greater than zero")
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
        expected_tier = BSC_TIER_BY_DISCOUNT[self.discount]
        if (
            self.base_services_charge_tier is not None
            and self.base_services_charge_tier != expected_tier
        ):
            # Schedule D-CARE sheet 1: "Customers whose otherwise applicable
            # rate includes a base services charge will be assigned to Tier 1."
            # FERA takes tier 2, the middle rate, matching its shallower
            # discount. Tier 3 is the default, so a CARE account left at it
            # pays $0.79343/day on E-ELEC against tier 1's $0.19713 -- about
            # $18 a month charged to an account the tariff puts on the
            # cheapest tier. Refused rather than corrected in place, matching
            # the acc_plus_segment guard above: silently rewriting a value the
            # caller set is harder to notice than being told.
            raise ConfigError(
                f"discount={self.discount!r} implies base_services_charge_tier="
                f"{expected_tier}; got {self.base_services_charge_tier}"
            )

    @property
    def resolved_bsc_tier(self) -> int:
        """Base Services Charge tier actually billed.

        Derived from the discount unless overridden, because the tariff ties
        the two together and nothing else did: the field used to default to
        tier 3, so a CARE account that simply did not mention it was billed
        $0.79343/day on E-ELEC instead of $0.19713 -- about $18 a month.
        """
        if self.base_services_charge_tier is not None:
            return self.base_services_charge_tier
        return BSC_TIER_BY_DISCOUNT[self.discount]

    @property
    def resolved_vintage(self) -> str:
        """The NBT vintage whose matrix applies to this customer.

        A year before the first vendored vintage floats, which is what the
        floating vintage is for. A year *after* the last one does not: it means
        the vintage table has not been vendored yet, and falling through to
        NBT00 produced an account that was floating for its energy value while
        still resolving an ACC Plus row -- locked for the adder, unlocked for
        everything else, with `lock_end` None and no warning anywhere.
        """
        if self.vintage is not None:
            return self.vintage
        assert self.interconnection_year is not None
        year = self.interconnection_year
        if year > max(VINTAGE_BY_YEAR):
            raise ConfigError(
                f"no NBT vintage is vendored for interconnection year {year}; "
                f"the newest is {VINTAGE_BY_YEAR[max(VINTAGE_BY_YEAR)]} "
                f"({max(VINTAGE_BY_YEAR)}). Set vintage= explicitly to override."
            )
        return VINTAGE_BY_YEAR.get(year, FLOATING_VINTAGE)

    @property
    def lock_end(self) -> date | None:
        """Last date covered by the nine-year rate lock, inclusive.

        ``None`` for a floating (NBT00) customer, who has no lock at all.

        A 29 February PTO has no anniversary in a common year, and 9 years on
        from a leap year always lands in one. It falls back to the 28th, which
        is what `trueup.relevant_period_end` does for the same reason -- and
        without it `is_locked` raises on every `price_at`, so such an account
        cannot price a single exported kWh.
        """
        if self.pto_date is None or self.resolved_vintage == FLOATING_VINTAGE:
            return None
        anniversary_year = self.pto_date.year + LOCK_YEARS
        try:
            anniversary = self.pto_date.replace(year=anniversary_year)
        except ValueError:  # 29 February in a common year
            anniversary = self.pto_date.replace(year=anniversary_year, day=28)
        return anniversary - _ONE_DAY

    def with_(self, **changes: Any) -> Config:
        return replace(self, **changes)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        data = dict(raw)
        # Heal a profile stored before the tier followed the discount. Every
        # such profile carries the old default of 3, because `to_dict` wrote
        # the field unconditionally, so a CARE or FERA account that was never
        # asked about a tier would now fail to load outright. Only the legacy
        # default is forgiven: an explicitly chosen tier that contradicts the
        # programme is still a configuration error worth hearing about.
        if (
            data.get("base_services_charge_tier") == _LEGACY_BSC_TIER
            and BSC_TIER_BY_DISCOUNT.get(str(data.get("discount", "none")), _LEGACY_BSC_TIER)
            != _LEGACY_BSC_TIER
        ):
            data["base_services_charge_tier"] = None
        unknown = set(data) - {f.name for f in cls.__dataclass_fields__.values()}
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        try:
            cca_value = data.get("cca")
            if isinstance(cca_value, dict):
                cca_raw = cca_value
                unknown_cca = set(cca_raw) - {
                    f.name for f in CcaConfig.__dataclass_fields__.values()
                }
                if unknown_cca:
                    raise ConfigError(f"unknown CCA config keys: {sorted(unknown_cca)}")
                data["cca"] = CcaConfig(**cca_raw)
            elif cca_value is not None and not isinstance(cca_value, CcaConfig):
                raise ConfigError("cca must be an object")
            if "supplier" in data:
                data["supplier"] = Supplier(data["supplier"])
            value = data.get("pto_date")
            if isinstance(value, str):
                data["pto_date"] = date.fromisoformat(value)
            value = data.get("smartrate_known_through")
            if isinstance(value, str):
                data["smartrate_known_through"] = date.fromisoformat(value)
            events = data.get("smartrate_events")
            if isinstance(events, list | tuple):
                data["smartrate_events"] = tuple(
                    date.fromisoformat(value) if isinstance(value, str) else value
                    for value in events
                )
            return cls(**data)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid config: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        """A JSON-compatible representation suitable for API request bodies."""
        data: dict[str, Any] = {
            "utility": self.utility.value,
            "tariff": self.tariff,
            "supplier": self.supplier.value,
            "interconnection_year": self.interconnection_year,
            "pto_date": self.pto_date.isoformat() if self.pto_date else None,
            "vintage": self.vintage,
            "acc_plus_segment": self.acc_plus_segment,
            "discount": self.discount,
            "base_services_charge_tier": self.base_services_charge_tier,
            "baseline_territory": self.baseline_territory,
            "baseline_code": self.baseline_code,
            "medical_baseline": self.medical_baseline,
            "medical_kwh_per_day": self.medical_kwh_per_day,
            "smartrate": self.smartrate,
            "smartrate_events": [event.isoformat() for event in self.smartrate_events],
            "smartrate_known_through": (
                self.smartrate_known_through.isoformat()
                if self.smartrate_known_through is not None
                else None
            ),
            "nsc_rate": self.nsc_rate,
        }
        if self.cca is not None:
            data["cca"] = {
                "name": self.cca.name,
                "rate_card": self.cca.rate_card,
                "option": self.cca.option,
                "pcia_vintage": self.cca.pcia_vintage,
                "pcia_rate": self.cca.pcia_rate,
                "franchise_fee_surcharge": self.cca.franchise_fee_surcharge,
                "generation_rates": self.cca.generation_rates,
                "export_generation_rate": self.cca.export_generation_rate,
            }
        return data

    @classmethod
    def from_toml(cls, path: str | Path) -> Config:
        with Path(path).open("rb") as handle:
            table = tomllib.load(handle)
        # The shared user config also carries integration settings such as the
        # default account profile and MQTT broker. They are not pricing fields
        # and must not make a stateless Config unusable.
        for section in ("account", "mqtt", "home_assistant", "influxdb"):
            table.pop(section, None)
        for key in ("profile", "default_profile", "account_profile"):
            table.pop(key, None)
        return cls.from_dict(table)

    @classmethod
    def from_env(cls, base: Config | None = None) -> Config:
        """Overlay ``TARIFFKIT_*`` environment variables onto ``base``."""
        config = base or cls()
        overrides: dict[str, Any] = {}
        if value := os.environ.get("TARIFFKIT_UTILITY"):
            overrides["utility"] = value
        if value := os.environ.get("TARIFFKIT_TARIFF"):
            overrides["tariff"] = value
        if value := os.environ.get("TARIFFKIT_SUPPLIER"):
            overrides["supplier"] = Supplier(value)
        if value := os.environ.get("TARIFFKIT_VINTAGE"):
            overrides["vintage"] = value
        if value := os.environ.get("TARIFFKIT_INTERCONNECTION_YEAR"):
            overrides["interconnection_year"] = int(value)
        if value := os.environ.get("TARIFFKIT_PTO_DATE"):
            overrides["pto_date"] = date.fromisoformat(value)
        if value := os.environ.get("TARIFFKIT_ACC_PLUS_SEGMENT"):
            overrides["acc_plus_segment"] = value
        if value := os.environ.get("TARIFFKIT_DISCOUNT"):
            overrides["discount"] = value
        if value := os.environ.get("TARIFFKIT_BSC_TIER"):
            overrides["base_services_charge_tier"] = int(value)
        if value := os.environ.get("TARIFFKIT_BASELINE_TERRITORY"):
            overrides["baseline_territory"] = value
        if value := os.environ.get("TARIFFKIT_BASELINE_CODE"):
            overrides["baseline_code"] = value
        if value := os.environ.get("TARIFFKIT_MEDICAL_BASELINE"):
            overrides["medical_baseline"] = _env_bool("TARIFFKIT_MEDICAL_BASELINE", value)
        if value := os.environ.get("TARIFFKIT_MEDICAL_KWH_PER_DAY"):
            overrides["medical_kwh_per_day"] = float(value)
        if value := os.environ.get("TARIFFKIT_SMARTRATE"):
            overrides["smartrate"] = _env_bool("TARIFFKIT_SMARTRATE", value)
        if value := os.environ.get("TARIFFKIT_SMARTRATE_EVENTS"):
            overrides["smartrate_events"] = tuple(
                date.fromisoformat(item.strip()) for item in value.split(",") if item.strip()
            )
        if value := os.environ.get("TARIFFKIT_SMARTRATE_KNOWN_THROUGH"):
            overrides["smartrate_known_through"] = date.fromisoformat(value)
        if value := os.environ.get("TARIFFKIT_NSC_RATE"):
            overrides["nsc_rate"] = float(value)
        if value := os.environ.get("TARIFFKIT_CCA_JSON"):
            try:
                cca_raw = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigError("TARIFFKIT_CCA_JSON is not valid JSON") from exc
            if not isinstance(cca_raw, dict):
                raise ConfigError("TARIFFKIT_CCA_JSON must be a JSON object")
            unknown_cca = set(cca_raw) - {f.name for f in CcaConfig.__dataclass_fields__.values()}
            if unknown_cca:
                raise ConfigError(f"unknown CCA config keys: {sorted(unknown_cca)}")
            overrides["cca"] = CcaConfig(**cca_raw)
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
    return base / "tariffkit" / "config.toml"
