"""Value types returned by the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .components import (
    EXPORT_GROUPS,
    IMPORT_GROUPS,
    ComponentGroup,
    group_components,
)
from .timeutil import DayType


class Season(StrEnum):
    SUMMER = "summer"
    WINTER = "winter"


class TouPeriod(StrEnum):
    PEAK = "peak"
    PART_PEAK = "part_peak"
    OFF_PEAK = "off_peak"


class Supplier(StrEnum):
    """Who supplies generation. PG&E delivers either way."""

    BUNDLED = "bundled"
    CCA = "cca"


class Utility(StrEnum):
    """Stable utility identities used by profiles and API payloads."""

    PACIFIC_GAS_AND_ELECTRIC = "pacific_gas_and_electric"
    PORTLAND_GENERAL_ELECTRIC = "pge"

    @property
    def data_slug(self) -> str:
        """Private filesystem key for this utility's vendored data."""
        match self:
            case Utility.PACIFIC_GAS_AND_ELECTRIC:
                return "pge"
            case Utility.PORTLAND_GENERAL_ELECTRIC:
                return "portland_general_electric"

    @property
    def short_name(self) -> str:
        """Recognizable user-facing abbreviation."""
        match self:
            case Utility.PACIFIC_GAS_AND_ELECTRIC:
                return "PG&E"
            case Utility.PORTLAND_GENERAL_ELECTRIC:
                return "PGE"

    @property
    def display_name(self) -> str:
        """Full user-facing company name."""
        match self:
            case Utility.PACIFIC_GAS_AND_ELECTRIC:
                return "Pacific Gas and Electric Company"
            case Utility.PORTLAND_GENERAL_ELECTRIC:
                return "Portland General Electric"


@dataclass(frozen=True, slots=True)
class ImportPrice:
    """What a kWh drawn from the grid costs, marginally."""

    total: float
    season: Season
    period: TouPeriod
    components: dict[str, float] = field(default_factory=dict)
    #: True when generation is supplied by a CCA and priced from their rate
    #: card rather than PG&E's tariff. Where that card is unconfigured, ``total``
    #: covers delivery only and ``complete`` is False.
    complete: bool = True
    #: Per-kWh credit available on usage within the cycle's baseline allowance,
    #: on schedules that have one (E-TOU-C). ``total`` is the over-baseline
    #: price, so subtract this for a kWh still inside the allowance.
    #:
    #: It is reported rather than applied because eligibility depends on
    #: cumulative usage, which a marginal price cannot know. The billing engine
    #: sees a whole cycle and applies it; 0.0 on schedules without a baseline.
    baseline_credit: float = 0.0

    def grouped(self) -> dict[ComponentGroup, float]:
        """``components`` rolled up into the fixed import groups.

        The groups sum back to ``total`` within per-component rounding, which is
        what makes them safe to draw as a stack against the price itself.
        """
        return group_components(self.components, IMPORT_GROUPS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "season": str(self.season),
            "period": str(self.period),
            "components": dict(self.components),
            "groups": {str(group): value for group, value in self.grouped().items()},
            "complete": self.complete,
            "baseline_credit": self.baseline_credit,
        }


@dataclass(frozen=True, slots=True)
class ExportPrice:
    """What a kWh pushed to the grid earns under the Net Billing Tariff."""

    total: float
    vintage: str
    day_type: DayType
    components: dict[str, float] = field(default_factory=dict)
    #: False once the timestamp passes the 9-year lock from the PTO date. PG&E
    #: publishes those years for illustration only; they are not guaranteed.
    locked: bool = True
    complete: bool = True
    #: False in far-future years where PG&E's own hour labels stop tracking
    #: Pacific daylight time and some holidays are duplicated onto the next day.
    #: Those years are well past any rate lock and are published for
    #: illustration only, but the value may be off by one hour slot.
    exact: bool = True

    def grouped(self) -> dict[ComponentGroup, float]:
        """``components`` rolled up into the fixed export groups."""
        return group_components(self.components, EXPORT_GROUPS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "vintage": self.vintage,
            "day_type": str(self.day_type),
            "components": dict(self.components),
            "groups": {str(group): value for group, value in self.grouped().items()},
            "locked": self.locked,
            "complete": self.complete,
            "exact": self.exact,
        }


@dataclass(frozen=True, slots=True)
class PricePoint:
    """Import and export prices for one clock hour."""

    start: datetime
    end: datetime
    import_price: ImportPrice
    export_price: ExportPrice

    @property
    def spread(self) -> float:
        """Export credit minus import cost.

        Positive means an exported kWh is worth more than the kWh it displaces,
        so exporting beats self-consumption for that hour.
        """
        return self.export_price.total - self.import_price.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "import": self.import_price.to_dict(),
            "export": self.export_price.to_dict(),
            "spread": round(self.spread, 6),
        }


@dataclass(frozen=True, slots=True)
class PriceCurve:
    """A contiguous run of hourly price points."""

    points: tuple[PricePoint, ...]

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Any:
        return iter(self.points)

    def __getitem__(self, index: int) -> PricePoint:
        return self.points[index]

    @property
    def start(self) -> datetime:
        return self.points[0].start

    @property
    def end(self) -> datetime:
        return self.points[-1].end

    def best_export_hours(self, n: int = 3) -> tuple[PricePoint, ...]:
        """The ``n`` hours with the highest export credit, earliest first."""
        ranked = sorted(self.points, key=lambda p: p.export_price.total, reverse=True)[:n]
        return tuple(sorted(ranked, key=lambda p: p.start))

    def cheapest_import_hours(self, n: int = 3) -> tuple[PricePoint, ...]:
        """The ``n`` cheapest hours to charge from the grid, earliest first."""
        ranked = sorted(self.points, key=lambda p: p.import_price.total)[:n]
        return tuple(sorted(ranked, key=lambda p: p.start))

    def peak_spread(self) -> PricePoint:
        """The hour where exporting beats self-consumption by the most."""
        return max(self.points, key=lambda p: p.spread)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "hours": len(self.points),
            "points": [p.to_dict() for p in self.points],
        }
