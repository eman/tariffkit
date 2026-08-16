"""Rate facade that resolves account history for every priced timestamp."""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta

from ..engine import RateEngine
from ..models import PriceCurve, PricePoint
from ..timeutil import hour_floor, now_pacific, to_pacific
from .model import AccountProfile


class AccountRateEngine:
    """Price a profile while selecting its epoch independently for each moment."""

    def __init__(self, profile: AccountProfile) -> None:
        self.profile = profile
        self._engines: dict[int, RateEngine] = {}

    def _engine_at(self, moment: datetime) -> RateEngine:
        pacific_date = to_pacific(moment).date()
        index = bisect_right(self.profile.effective_dates, pacific_date) - 1
        if index < 0:
            self.profile.config_at(moment)
            raise AssertionError("config_at should have raised for prehistory")
        engine = self._engines.get(index)
        if engine is None:
            engine = RateEngine(self.profile.epochs[index].config)
            self._engines[index] = engine
        return engine

    def price_at(self, moment: datetime) -> PricePoint:
        """Price the clock hour containing ``moment`` under its active epoch."""
        return self._engine_at(moment).price_at(moment)

    def price_now(self) -> PricePoint:
        return self.price_at(now_pacific())

    def forecast(self, hours: int = 24, start: datetime | None = None) -> PriceCurve:
        """Forecast across transitions without applying one config to all hours."""
        if hours < 1:
            raise ValueError("hours must be >= 1")
        cursor = hour_floor(to_pacific(start) if start else now_pacific())
        base = cursor.astimezone(UTC)
        return PriceCurve(
            tuple(self.price_at(base + timedelta(hours=offset)) for offset in range(hours))
        )

    def daily_fixed_charge(self, moment: datetime | None = None) -> float:
        """Return the fixed charge from the epoch active at ``moment``."""
        when = moment or now_pacific()
        return self._engine_at(when).daily_fixed_charge(when)

    def describe(self, moment: datetime | None = None) -> dict[str, object]:
        """Return rate provenance plus the active profile epoch."""
        when = moment or now_pacific()
        description = self._engine_at(when).describe()
        description["account_profile"] = self.profile.name or None
        description["account_effective"] = self.profile.config_at(when).to_dict()
        return description
