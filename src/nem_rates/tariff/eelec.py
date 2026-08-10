"""PG&E Schedule E-ELEC import pricing.

E-ELEC is simpler than most residential TOU schedules: the period boundaries are
identical every day of the week including holidays, they do not shift by season,
and there is no baseline allowance or tier structure. So a price is fully
determined by (season, hour).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any

from ..cca import load_rate_card
from ..config import Config
from ..errors import ConfigError, DataError
from ..models import ImportPrice, Season, Supplier, TouPeriod

TARIFF_DATA_ROOT = "tariff"


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
def _snapshots(utility: str, tariff: str) -> tuple[TariffSnapshot, ...]:
    directory = (
        files("nem_rates.data")
        / TARIFF_DATA_ROOT
        / utility.lower()
        / tariff.lower().replace("-", "")
    )
    if not directory.is_dir():
        raise DataError(f"no vendored tariff data for {utility}/{tariff}")
    found = []
    for entry in directory.iterdir():
        if not entry.name.endswith(".toml"):
            continue
        raw = tomllib.loads(entry.read_text(encoding="utf-8"))
        found.append(TariffSnapshot(date.fromisoformat(raw["effective"]), raw))
    if not found:
        raise DataError(f"no tariff snapshots for {utility}/{tariff}")
    return tuple(sorted(found, key=lambda s: s.effective))


def load_snapshot(utility: str, tariff: str, on: date) -> TariffSnapshot:
    """The snapshot in force on ``on`` -- the latest one not in the future."""
    snapshots = _snapshots(utility, tariff)
    applicable = [s for s in snapshots if s.effective <= on]
    if not applicable:
        raise DataError(
            f"{utility}/{tariff}: no snapshot effective on or before {on}; "
            f"earliest vendored is {snapshots[0].effective}"
        )
    return applicable[-1]


class EelecTariff:
    """Prices a kWh of grid import under E-ELEC."""

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
        """Resolve the TOU period. Identical every day of the week."""
        snapshot = snapshot or self.snapshot_for(moment)
        periods = snapshot.raw["periods"]
        hour = moment.hour
        for start, end in periods["peak"]:
            if start <= hour < end:
                return TouPeriod.PEAK
        for start, end in periods["part_peak"]:
            if start <= hour < end:
                return TouPeriod.PART_PEAK
        return TouPeriod.OFF_PEAK

    def price_at(self, moment: datetime) -> ImportPrice:
        snapshot = self.snapshot_for(moment)
        season = self.season(moment, snapshot)
        period = self.period(moment, snapshot)
        components, complete = self._components(snapshot, season, period, moment)
        total = self._apply_discount(snapshot, components)
        return ImportPrice(
            total=round(total, 6),
            season=season,
            period=period,
            components={k: round(v, 6) for k, v in components.items()},
            complete=complete,
        )

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
                card = load_rate_card(cca.rate_card)
                components["cca_generation"] = card.generation(str(season), str(period), cca.option)
                credit = card.cost_relief_credit(moment.date())
                if credit:
                    components["cca_cost_relief_credit"] = credit
            else:
                # Delivery-only price. Flagged rather than silently understated.
                complete = False

        return components, complete

    def _apply_discount(self, snapshot: TariffSnapshot, components: dict[str, float]) -> float:
        total = sum(components.values())
        if self.config.discount == "none":
            return total
        discounts = snapshot.raw["discounts"]
        rate = float(discounts[self.config.discount])
        if self.config.discount == "care":
            # The Wildfire Fund Charge is not levied on CARE sales at all, so it
            # comes off before the percentage discount rather than being
            # discounted alongside everything else.
            for name in discounts.get("care_excludes", []):
                if name in components:
                    total -= components.pop(name)
        return total * (1.0 - rate)

    def daily_fixed_charge(self, moment: datetime) -> float:
        """Base Services Charge in $/day.

        Kept out of the per-kWh price on purpose: it does not vary with
        consumption, so folding it in would corrupt any marginal decision about
        whether to import, export, or self-consume.

        The CARE/FERA reduction is already baked into the tier 1 and tier 2
        amounts, so the percentage discount is not applied again here.
        """
        snapshot = self.snapshot_for(moment)
        return float(
            snapshot.raw["base_services_charge"][f"tier_{self.config.base_services_charge_tier}"]
        )
