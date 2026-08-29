"""PG&E residential retail import pricing.

Schedule-agnostic: everything that differs between schedules lives in the
vendored snapshot. The active residential portfolio is vendored: E-1, E-ELEC,
E-TOU-C, E-TOU-D, and EV2-A. E-TOU-D's peak applies only on non-holiday
weekdays; the other TOU schedules use the same periods every day.

E-1 and E-TOU-C have a baseline credit, which applies to the first N
kWh of a cycle. That is a quantity rather than a time, so no marginal price can
express it. ``price_at`` returns the over-baseline price -- the right answer for
a dispatch decision, since an allowance is normally spent early in the cycle --
and reports the available credit as ``ImportPrice.baseline_credit`` for the
billing engine, which sees a whole cycle, to apply.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any, Final

from ..cca import load_rate_card
from ..config import Config
from ..data.versioned import load as load_version
from ..errors import ConfigError, DataError
from ..models import ImportPrice, Season, Supplier, TouPeriod, Utility
from ..timeutil import DayType, day_type

TARIFF_DATA_ROOT = "tariff"
SUPPORTED_TARIFFS: Final[tuple[str, ...]] = (
    "E-1",
    "E-ELEC",
    "E-TOU-C",
    "E-TOU-D",
    "EV2-A",
)
FERA_DISCOUNT = 0.18


def load_program(name: str, on: date) -> dict[str, Any]:
    """One generated residential-program vintage."""
    return load_version(f"tariff/pge/program/{name}", on, label=name).raw


@dataclass(frozen=True, slots=True)
class TariffSnapshot:
    """One effective-dated version of a tariff sheet."""

    effective: date
    raw: dict[str, Any]

    @property
    def advice_letter(self) -> str:
        return str(self.raw.get("advice_letter", ""))

    @property
    def source_url(self) -> str:
        return str(self.raw.get("source_url", ""))


@lru_cache(maxsize=8)
def _snapshots(utility: Utility, tariff: str) -> tuple[TariffSnapshot, ...]:
    directory = (
        files("tariffkit.data")
        / TARIFF_DATA_ROOT
        / utility.data_slug
        / tariff.lower().replace("-", "")
    )
    if not directory.is_dir():
        raise DataError(f"no vendored tariff data for {utility}/{tariff}")
    found = []
    for entry in directory.iterdir():
        if not entry.name.endswith(".toml"):
            continue
        raw = tomllib.loads(entry.read_text(encoding="utf-8"))
        try:
            raw["utility"] = Utility(raw["utility"]).value
        except (KeyError, ValueError) as exc:
            raise DataError(f"{entry.name}: invalid utility identifier") from exc
        found.append(TariffSnapshot(date.fromisoformat(raw["effective"]), raw))
    if not found:
        raise DataError(f"no tariff snapshots for {utility}/{tariff}")
    return tuple(sorted(found, key=lambda s: s.effective))


def load_snapshot(utility: Utility | str, tariff: str, on: date) -> TariffSnapshot:
    """The snapshot in force on ``on`` -- the latest one not in the future."""
    utility_id = Utility(utility)
    snapshots = _snapshots(utility_id, tariff)
    applicable = [s for s in snapshots if s.effective <= on]
    if not applicable:
        raise DataError(
            f"{utility_id.value}/{tariff}: no snapshot effective on or before {on}; "
            f"earliest vendored is {snapshots[0].effective}"
        )
    return applicable[-1]


class RetailTariff:
    """Prices a kWh of grid import under the configured schedule."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def snapshot_for(self, moment: datetime) -> TariffSnapshot:
        return load_snapshot(self.config.utility, self.config.tariff, moment.date())

    def season(self, moment: datetime, snapshot: TariffSnapshot | None = None) -> Season:
        snapshot = snapshot or self.snapshot_for(moment)
        seasons = snapshot.raw["seasons"]
        start_month, start_day = (int(p) for p in seasons["summer_start"].split("-"))
        end_month, end_day = (int(p) for p in seasons["summer_end"].split("-"))
        marker = (moment.month, moment.day)
        if (start_month, start_day) <= marker <= (end_month, end_day):
            return Season.SUMMER
        return Season.WINTER

    def period(self, moment: datetime, snapshot: TariffSnapshot | None = None) -> TouPeriod:
        """Resolve the TOU period, including schedule-defined day restrictions."""
        snapshot = snapshot or self.snapshot_for(moment)
        periods = snapshot.raw["periods"]
        if periods.get("peak_weekdays_only") and day_type(moment) is DayType.WEEKEND:
            return TouPeriod.OFF_PEAK
        hour = moment.hour
        for start, end in periods["peak"]:
            if start <= hour < end:
                return TouPeriod.PEAK
        # Absent rather than empty on a schedule with no part-peak, which is how
        # E-TOU-C is written: peak or off-peak, nothing between.
        for start, end in periods.get("part_peak", []):
            if start <= hour < end:
                return TouPeriod.PART_PEAK
        return TouPeriod.OFF_PEAK

    def price_at(self, moment: datetime) -> ImportPrice:
        snapshot = self.snapshot_for(moment)
        season = self.season(moment, snapshot)
        period = self.period(moment, snapshot)
        components, complete = self._components(snapshot, season, period, moment)
        if self.config.smartrate:
            smart_rate = load_program("ersmart", moment.date())
            if self.config.smartrate_known_through is None or (
                moment.date() > self.config.smartrate_known_through
            ):
                complete = False
            if moment.date() in self.config.smartrate_events and int(
                smart_rate["high_price_start"]
            ) <= moment.hour < int(smart_rate["high_price_end"]):
                components["smartrate_high_price"] = float(smart_rate["high_price_charge"])
        total = self._apply_discount(snapshot, components, season, period, moment)
        return ImportPrice(
            total=round(total, 6),
            season=season,
            period=period,
            components={k: round(v, 6) for k, v in components.items()},
            complete=complete,
            baseline_credit=float(snapshot.raw.get("baseline", {}).get("credit", 0.0)),
        )

    def baseline_allowance(self, moment: datetime) -> float:
        """Baseline kWh allowed for the day containing ``moment``.

        Zero on a schedule without a baseline, and zero when no territory is
        configured -- the quantities vary several-fold between territories, so
        guessing one would be worse than reporting no allowance.
        """
        snapshot = self.snapshot_for(moment)
        quantities = snapshot.raw.get("baseline", {}).get("quantities")
        if not quantities or self.config.baseline_territory is None:
            return 0.0
        table = quantities.get(self.config.baseline_code)
        if table is None:
            raise ConfigError(
                f"unknown baseline_code {self.config.baseline_code!r}; "
                f"vendored: {sorted(quantities)}"
            )
        territory = table.get(self.config.baseline_territory.upper())
        if territory is None:
            raise ConfigError(
                f"no baseline quantity for territory "
                f"{self.config.baseline_territory!r}; vendored: {sorted(table)}"
            )
        allowance = float(territory[str(self.season(moment, snapshot))])
        if self.config.medical_baseline:
            medical = load_program("medicalbaseline", moment.date())
            standard = float(medical["standard_kwh_per_year"]) / 365
            allowance += self.config.medical_kwh_per_day or standard
        return allowance

    def _components(
        self,
        snapshot: TariffSnapshot,
        season: Season,
        period: TouPeriod,
        moment: datetime,
    ) -> tuple[dict[str, float], bool]:
        energy = snapshot.raw["energy"][str(season)][str(period)]
        components: dict[str, float] = dict(energy)
        components.update(snapshot.raw["adders"])
        complete = True

        if self.config.supplier is Supplier.CCA:
            cca = self.config.cca
            assert cca is not None
            for name in snapshot.raw["cca"]["drop_components"]:
                components.pop(name, None)

            # A rate taken straight from a bill wins over the vintage table,
            # which only carries the vintages printed on the E-ELEC sheet.
            if cca.pcia_rate is not None:
                components["pcia"] = cca.pcia_rate
            elif cca.pcia_vintage is not None:
                table = snapshot.raw["cca"]["pcia_vintages"]
                key = str(cca.pcia_vintage)
                if key not in table:
                    raise ConfigError(
                        f"no PCIA rate vendored for vintage {cca.pcia_vintage}; "
                        f"available: {sorted(table)}"
                    )
                components["pcia"] = float(table[key])

            # Same precedence as the PCIA above: an explicit rate wins, then the
            # vendored Schedule E-FFS table, which is vintaged off the same year.
            if cca.franchise_fee_surcharge is not None:
                components["franchise_fee_surcharge"] = cca.franchise_fee_surcharge
            elif cca.pcia_vintage is not None:
                ffs_table = snapshot.raw["cca"]["franchise_fee_vintages"]
                key = str(cca.pcia_vintage)
                if key not in ffs_table:
                    # Reaching here means the two vintaged tables disagree, since
                    # the PCIA lookup above already accepted this year. That is a
                    # vendoring bug, not a gap in the user's config, so it raises
                    # rather than degrading to complete=False -- which means
                    # something quite different and much larger.
                    raise ConfigError(
                        f"vintage {cca.pcia_vintage} has a PCIA rate but no franchise fee "
                        f"surcharge; vendored franchise fee vintages: {sorted(ffs_table)}"
                    )
                components["franchise_fee_surcharge"] = float(ffs_table[key])
            else:
                complete = False

            rates = cca.generation_rates.get(str(season), {})
            if str(period) in rates:
                components["cca_generation"] = float(rates[str(period)])
            elif cca.rate_card is not None:
                card = load_rate_card(cca.rate_card, moment.date())
                components["cca_generation"] = card.generation(
                    self.config.tariff, str(season), str(period), cca.option
                )
                credit = card.cost_relief_credit(moment.date())
                if credit:
                    components["cca_cost_relief_credit"] = credit
            else:
                # Delivery-only price. Flagged rather than silently understated.
                complete = False

        return components, complete

    def _apply_discount(
        self,
        snapshot: TariffSnapshot,
        components: dict[str, float],
        season: Season,
        period: TouPeriod,
        moment: datetime,
    ) -> float:
        medical_credit = 0.0
        if self.config.medical_baseline:
            program_name = (
                "dmedical"
                if self.config.tariff in {"E-ELEC", "E-TOU-D", "EV2-A"}
                else "medicalbaseline"
            )
            medical = load_program(program_name, moment.date())
            exemptions = (
                medical.get("exempt_components", [])
                if program_name == "dmedical"
                else snapshot.raw.get("medical", {}).get("exempt_components", [])
            )
            for name in exemptions:
                components.pop(str(name), None)
            if program_name == "dmedical":
                bundled = dict(snapshot.raw["energy"][str(season)][str(period)])
                bundled.update(snapshot.raw["adders"])
                for name in medical["exempt_components"]:
                    bundled.pop(str(name), None)
                medical_credit = sum(bundled.values()) * float(medical["discount"])

        total = sum(components.values())
        if self.config.discount != "none":
            if self.config.discount == "care":
                care = load_program("dcare", moment.date())
                rate = float(care["discount"])
                for name in care["exempt_components"]:
                    name = str(name)
                    if name in components:
                        total -= components.pop(name)
            else:
                rate = FERA_DISCOUNT
            components[f"{self.config.discount}_discount"] = -total * rate
        if medical_credit:
            components["medical_discount"] = -medical_credit
        return sum(components.values())

    def daily_fixed_charge(self, moment: datetime) -> float:
        """Base Services Charge in $/day.

        Kept out of the per-kWh price on purpose: it does not vary with
        consumption, so folding it in would corrupt any marginal decision about
        whether to import, export, or self-consume.

        The CARE/FERA reduction is already baked into the tier 1 and tier 2
        amounts, so the percentage discount is not applied again here.

        Zero when the vintage being priced has no such charge. AB 205's charge
        began on 2026-03-01; before it, E-TOU-C and EV2-A had no daily fixed
        charge at all, and E-ELEC had a single flat per-meter rate. A snapshot
        from then carries no table, and that absence is the correct answer
        rather than a missing-data error.
        """
        snapshot = self.snapshot_for(moment)
        table = snapshot.raw.get("base_services_charge")
        if not table:
            return 0.0
        return float(table[f"tier_{self.config.resolved_bsc_tier}"])
