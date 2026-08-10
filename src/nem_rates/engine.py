"""The composed rate engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import Config
from .export.nbt import NbtExportRates
from .models import PriceCurve, PricePoint
from .tariff.eelec import EelecTariff
from .timeutil import PACIFIC, hour_floor, now_pacific, to_pacific


class RateEngine:
    """Import and export prices for one service agreement.

    Both sides are static published tables, so every lookup is pure and O(1) and
    ``forecast`` is exact rather than predictive -- it is reading ahead in a
    schedule, not modelling one.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.tariff = EelecTariff(self.config)
        self.export_rates = NbtExportRates(self.config)

    def price_at(self, moment: datetime) -> PricePoint:
        """Prices for the clock hour containing ``moment``."""
        pacific = hour_floor(to_pacific(moment))
        # Step in UTC for the same reason ``forecast`` does. Adding an hour to
        # the zoned start does wall-clock arithmetic, so on the fall-back day
        # the first 01:00 lands on 02:00 PST -- two real hours later, fully
        # overlapping the second 01:00.
        end = (pacific.astimezone(UTC) + timedelta(hours=1)).astimezone(PACIFIC)
        return PricePoint(
            start=pacific,
            end=end,
            import_price=self.tariff.price_at(pacific),
            export_price=self.export_rates.price_at(pacific),
        )

    def price_now(self) -> PricePoint:
        return self.price_at(now_pacific())

    def forecast(self, hours: int = 24, start: datetime | None = None) -> PriceCurve:
        """The next ``hours`` hourly price points, starting with the current one.

        Steps by absolute time, so DST transitions produce the right number of
        distinct hours: the fall-back day yields two 01:00 entries at different
        offsets, and the spring-forward day skips 02:00 entirely.
        """
        if hours < 1:
            raise ValueError("hours must be >= 1")
        cursor = hour_floor(to_pacific(start) if start else now_pacific())
        # Step in UTC. Adding a timedelta directly to a zoned datetime does
        # wall-clock arithmetic, which lands on the nonexistent 02:00 in spring
        # and silently repeats an hour in autumn.
        base = cursor.astimezone(UTC)
        points = [self.price_at(base + timedelta(hours=offset)) for offset in range(hours)]
        return PriceCurve(tuple(points))

    def daily_fixed_charge(self, moment: datetime | None = None) -> float:
        """Base Services Charge in $/day, excluded from the per-kWh prices."""
        return self.tariff.daily_fixed_charge(to_pacific(moment or now_pacific()))

    def describe(self) -> dict[str, object]:
        """Provenance for the data backing this engine."""
        snapshot = self.tariff.snapshot_for(now_pacific())
        low, high = self.export_rates.covered_years
        return {
            "utility": self.config.utility,
            "tariff": self.config.tariff,
            "supplier": str(self.config.supplier),
            "tariff_effective": snapshot.effective.isoformat(),
            "tariff_advice_letter": snapshot.advice_letter,
            "tariff_source": snapshot.source_url,
            "export_vintage": self.export_rates.vintage,
            "export_years": [low, high],
            "acc_plus": self.export_rates.acc_plus,
            "lock_end": self.config.lock_end.isoformat() if self.config.lock_end else None,
        }
