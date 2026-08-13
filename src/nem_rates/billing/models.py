"""Value types for bill computation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..models import Season, TouPeriod
from ..timeutil import PACIFIC, to_pacific


@dataclass(frozen=True, slots=True)
class IntervalReading:
    """Metered energy over one interval.

    ``imported`` and ``exported`` are what crossed the meter, in kWh. Under the
    Net Billing Tariff the meter has already netted within the interval, so in
    real AMI data at most one of them is non-zero. Both are kept because the
    distinction is what the tariff prices differently, and because gross
    inverter data has to be netted before it can be billed.
    """

    start: datetime
    imported: float = 0.0
    exported: float = 0.0
    duration: timedelta = timedelta(hours=1)
    #: True when this interval's energy was reconstructed across a gap in the
    #: source rather than measured over the interval itself. The total stays
    #: right -- a cumulative counter only depends on its endpoints -- but the
    #: shape does not, and the shape is what a time-of-use tariff prices. A
    #: three-day outage spread evenly gives peak hours 5/24 of the energy where
    #: the real day gives them nearly a third, which is a real dollar on a
    #: cycle whose total is exact to 0.05 kWh.
    estimated: bool = False

    def __post_init__(self) -> None:
        if self.imported < 0 or self.exported < 0:
            raise ValueError(
                f"readings must be non-negative; got imported={self.imported}, "
                f"exported={self.exported}. Net readings should go through "
                f"IntervalReading.from_net()."
            )
        if self.duration <= timedelta(0):
            raise ValueError(f"duration must be positive, got {self.duration}")

    @property
    def end(self) -> datetime:
        return self.start + self.duration

    @property
    def net(self) -> float:
        """Positive when importing, negative when exporting."""
        return self.imported - self.exported

    @classmethod
    def from_net(
        cls, start: datetime, net_kwh: float, duration: timedelta = timedelta(hours=1)
    ) -> IntervalReading:
        """Build from a single signed value, positive meaning import."""
        if net_kwh >= 0:
            return cls(start, imported=net_kwh, duration=duration)
        return cls(start, exported=-net_kwh, duration=duration)

    @classmethod
    def from_gross(
        cls,
        start: datetime,
        consumption_kwh: float,
        production_kwh: float,
        duration: timedelta = timedelta(hours=1),
    ) -> IntervalReading:
        """Net gross site load against gross generation.

        Use for inverter or CT-clamp data, which reports both sides
        independently. Real AMI data is already netted -- do not double-net it.
        """
        return cls.from_net(start, consumption_kwh - production_kwh, duration)


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    """A meter-read-to-meter-read cycle.

    Both ends are inclusive dates, matching how a statement prints them. The
    Base Services Charge is billed per day over ``days``, which is why this is
    not a calendar month -- a real cycle is 27 to 33 days.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period ends before it starts: {self.start} to {self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, moment: datetime) -> bool:
        return self.start <= to_pacific(moment).date() <= self.end

    @property
    def elapsed(self) -> timedelta:
        """Real time the cycle spans, which is not ``days`` times 24 hours.

        A cycle containing a DST transition is an hour longer or shorter. Use
        this to ask how much metered data *should* be there; use ``days`` for
        anything billed per calendar day, like the Base Services Charge.
        """
        opens = datetime(self.start.year, self.start.month, self.start.day, tzinfo=PACIFIC)
        # Wall-clock arithmetic is right here: the cycle closes at the next local
        # midnight, however many real hours away that falls.
        closes = datetime(self.end.year, self.end.month, self.end.day, tzinfo=PACIFIC) + timedelta(
            days=1
        )
        return closes.astimezone(UTC) - opens.astimezone(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
        }

    @classmethod
    def from_readings(cls, readings: Sequence[IntervalReading]) -> BillingPeriod:
        """Infer the cycle from the data's own span."""
        if not readings:
            raise ValueError("cannot infer a billing period from no readings")
        starts = [to_pacific(r.start) for r in readings]
        return cls(min(starts).date(), max(starts).date())


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """Energy in one season and TOU period, at one rate.

    Mirrors a printed bill line -- "Off Peak 22.903000 kWh @ $0.11878" -- so a
    computed bill can be compared against a statement line by line.
    """

    season: Season
    period: TouPeriod
    imported: float = 0.0
    exported: float = 0.0
    import_charge: float = 0.0
    export_credit: float = 0.0

    @property
    def import_rate(self) -> float | None:
        """Effective $/kWh, or None when nothing was imported."""
        return self.import_charge / self.imported if self.imported else None

    @property
    def export_rate(self) -> float | None:
        return self.export_credit / self.exported if self.exported else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": str(self.season),
            "period": str(self.period),
            "imported_kwh": round(self.imported, 6),
            "exported_kwh": round(self.exported, 6),
            "import_charge": round(self.import_charge, 4),
            "export_credit": round(self.export_credit, 4),
            "import_rate": None if self.import_rate is None else round(self.import_rate, 5),
            "export_rate": None if self.export_rate is None else round(self.export_rate, 5),
        }


#: Per-kWh charges a statement prints as taxes rather than as energy charges.
#: In ``import_components`` and in ``total``, but outside ``energy_charges``.
TAX_COMPONENTS = frozenset({"energy_commission_tax"})


@dataclass(frozen=True, slots=True)
class Bill:
    """A computed statement.

    Charges are positive, credits negative, so every collection here sums
    directly into ``total``.
    """

    period: BillingPeriod
    buckets: tuple[UsageBucket, ...] = ()
    #: Import charges by rate component, e.g. distribution, cca_generation.
    import_components: dict[str, float] = field(default_factory=dict)
    #: Export credits by component, e.g. delivery, cca_generation, acc_plus.
    export_components: dict[str, float] = field(default_factory=dict)
    #: Charges that do not scale with energy, e.g. the Base Services Charge.
    fixed_components: dict[str, float] = field(default_factory=dict)
    #: Set when the readings did not cover the period cleanly. Independent of
    #: ``complete``: patchy meter data does not make the rates uncertain.
    warnings: tuple[str, ...] = ()
    #: False when any priced hour was itself incomplete or inexact -- a statement
    #: about the *rates*, not the readings. A bill can be fully priced and still
    #: carry coverage warnings, or cover the period perfectly and still be priced
    #: from an unverified CCA export credit. Check both before trusting a total.
    complete: bool = True

    @property
    def imported_kwh(self) -> float:
        return sum(b.imported for b in self.buckets)

    @property
    def exported_kwh(self) -> float:
        return sum(b.exported for b in self.buckets)

    @property
    def energy_charges(self) -> float:
        """The per-kWh charges for energy, as a statement's own lines total.

        Excludes statutory taxes, which are per-kWh but not energy charges and
        print on their own line: the July 2026 statement's six energy lines come
        to $8.90 and its Energy Commission Tax is separate. They are still in
        ``import_components`` and still in ``total`` -- they are simply not what
        this figure means.
        """
        return sum(
            value for name, value in self.import_components.items() if name not in TAX_COMPONENTS
        )

    @property
    def export_credits(self) -> float:
        """Negative: credits reduce the bill."""
        return sum(self.export_components.values())

    @property
    def fixed_charges(self) -> float:
        return sum(self.fixed_components.values())

    @property
    def total(self) -> float:
        return self.energy_charges + self.taxes + self.export_credits + self.fixed_charges

    @property
    def taxes(self) -> float:
        """Statutory per-kWh charges, which a statement prints on their own line.

        Separate from ``energy_charges`` because they are not charges for energy
        and a statement does not total them with the energy lines -- but they are
        owed, so they are in ``total``. Splitting them out of ``energy_charges``
        without adding them here dropped them from the bill entirely, which is
        what the round-trip test now pins.
        """
        return sum(
            value for name, value in self.import_components.items() if name in TAX_COMPONENTS
        )

    @property
    def effective_import_rate(self) -> float | None:
        """Blended $/kWh actually paid for energy, credits included.

        Not a marginal rate -- do not dispatch on it.
        """
        return self.energy_charges / self.imported_kwh if self.imported_kwh else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period.to_dict(),
            "imported_kwh": round(self.imported_kwh, 4),
            "exported_kwh": round(self.exported_kwh, 4),
            "buckets": [b.to_dict() for b in self.buckets],
            "import_components": {k: round(v, 4) for k, v in self.import_components.items()},
            "export_components": {k: round(v, 4) for k, v in self.export_components.items()},
            "fixed_components": {k: round(v, 4) for k, v in self.fixed_components.items()},
            "energy_charges": round(self.energy_charges, 2),
            "export_credits": round(self.export_credits, 2),
            "fixed_charges": round(self.fixed_charges, 2),
            "total": round(self.total, 2),
            "complete": self.complete,
            "warnings": list(self.warnings),
        }
