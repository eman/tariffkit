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
#: Which vendored programme sheet carries each discount's rate and exemptions.
#: FERA used to be a hardcoded 0.18 with no exemptions at all, so a FERA bill
#: was discounted over a base that wrongly included the wildfire hardening and
#: both recovery bond line items.
DISCOUNT_PROGRAM = {"care": "dcare", "fera": "efera"}


def discount_terms(discount: str, on: date) -> tuple[float, list[str]]:
    """The rate and exempt components for a discount programme on a date.

    Both come from the vendored sheet, and the two lists genuinely differ:
    FERA is not exempt from the Wildfire Fund Charge, which CARE is, so neither
    can stand in for the other.

    One CARE exemption is not modelled. Advice 7846-E added "the CARE surcharge
    portion of the public purpose program charge used to fund the CARE
    discount" to D-CARE sheet 1, and PG&E publishes no separate rate for that
    portion -- only the whole public purpose programs charge -- so it cannot be
    subtracted here. The CARE discount is therefore taken on a base that still
    includes it, and comes out slightly large. Order $0.50 a month at 500 kWh
    on E-TOU-C, in the customer's favour.

    The same sheet excludes the California Climate Credit from discounting.
    That one costs nothing: the engine bills no such component -- it appears
    only as a statement-level adjustment -- so there is nothing to exclude.
    """
    if discount == "none":
        return 0.0, []
    program = load_program(DISCOUNT_PROGRAM[discount], on)
    return float(program["discount"]), [str(name) for name in program["exempt_components"]]


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
            baseline_credit=self._discounted_baseline_credit(snapshot, moment),
        )

    def _discounted_baseline_credit(self, snapshot: Any, moment: datetime) -> float:
        """Baseline credit in $/kWh, carrying the same discount as the charges.

        D-CARE takes its discount "on their total bundled volumetric charges",
        and E-TOU-C sheet 2 prints "Baseline Credit (Applied to Baseline Usage
        Only)" inside its TOTAL BUNDLED RATES table -- so the credit is part of
        that total and carries the discount with everything else. (The sheet's
        other clause, that discounts "will be applied as a reduction to
        distribution charges", says where the discount lands on the bill, not
        what is in its base; it does not support this and is not the authority
        for it.)

        Read raw and applied at bill level at full value while every charge
        around it was scaled, a CARE customer received an undiscounted credit
        against discounted charges: on a 250 kWh within-baseline E-TOU-C
        January that was $46.29 against $54.66, 18% of the bill.
        """
        credit = float(snapshot.raw.get("baseline", {}).get("credit", 0.0))
        if not credit or self.config.discount == "none":
            return credit
        rate, _exempt = discount_terms(self.config.discount, moment.date())
        return credit * (1.0 - rate)

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
                # Bundled-equivalent for a CCA account too, which D-MEDICAL
                # states in the same words D-CARE and E-FERA use: "the MEDICAL
                # discount will be calculated for direct access and community
                # choice aggregation customers based on the total charges as if
                # they were subject to bundled service rates."
                base = self._bundled_equivalent(
                    snapshot,
                    season,
                    period,
                    [str(name) for name in medical["exempt_components"]],
                )
                medical_credit = base * float(medical["discount"])

        total = sum(components.values())
        if self.config.discount != "none":
            rate, exempt = discount_terms(self.config.discount, moment.date())
            for name in exempt:
                if name in components:
                    total -= components.pop(name)
            if self.config.supplier is Supplier.CCA:
                # D-CARE sheet 1 and E-FERA sheet 1, identically: "The discount
                # will be calculated for direct access and community choice
                # aggregation customers based on the total charges as if they
                # were subject to bundled service rates." Discounting the CCA
                # stack instead -- cca_generation plus a vintaged PCIA plus the
                # franchise fee surcharge -- made the base several cents per
                # kWh too high, so the credit came out too large. D-MEDICAL
                # below has always rebuilt the bundled base for this reason.
                total = self._bundled_equivalent(snapshot, season, period, exempt)
            components[f"{self.config.discount}_discount"] = -total * rate
        if medical_credit:
            components["medical_discount"] = -medical_credit
        return sum(components.values())

    def _bundled_equivalent(
        self,
        snapshot: Any,
        season: Season,
        period: TouPeriod,
        exempt: list[str],
    ) -> float:
        """Total volumetric charges as if the customer took bundled service.

        The sheet's own words for a CCA or direct-access customer. Built from
        the snapshot's bundled energy rate plus its adders, less whatever the
        programme exempts -- the same construction D-MEDICAL uses two branches
        down, which is why the two now agree.
        """
        bundled = dict(snapshot.raw["energy"][str(season)][str(period)])
        bundled.update(snapshot.raw["adders"])
        for name in exempt:
            bundled.pop(name, None)
        return sum(float(value) for value in bundled.values())

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
