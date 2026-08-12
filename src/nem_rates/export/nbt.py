"""Export credit lookup against the vendored NBT matrices.

PG&E's published file is 20 years of hourly rows, but every value repeats across
a 576-cell matrix per year (12 months x 2 day types x 24 hours) per component.
``tools/regen_data.py`` collapses it; this module reads the collapsed form, so a
lookup is a few list indexes rather than a scan of 350,640 rows.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from typing import Any

from ..config import FLOATING_VINTAGE, Config
from ..data import read_data_json_gz, versioned
from ..errors import ConfigError, DataError, OutOfRangeError
from ..models import ExportPrice, Supplier
from ..timeutil import MONTHS, DayType, day_type, export_hour

#: Where a utility's ACC Plus adder table lives. Keyed by utility rather than
#: hardcoded to one, and effective-dated, so a revision sits beside its
#: predecessor instead of overwriting it.
ACC_PLUS_DIR = "export/{utility}/acc_plus"


@lru_cache(maxsize=8)
def _matrix(vintage: str) -> dict[str, Any]:
    payload = read_data_json_gz(f"export/pge/{vintage.lower()}.json.gz")
    if payload.get("schema") != 1:
        raise DataError(f"{vintage}: unsupported data schema {payload.get('schema')}")
    return payload


def _acc_plus_table(utility: str, on: date) -> dict[str, Any]:
    relative = ACC_PLUS_DIR.format(utility=utility.lower())
    return versioned.load(relative, on, label=f"{utility} ACC Plus").raw


class NbtExportRates:
    """Prices a kWh exported to the grid under the Net Billing Tariff."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.vintage = config.resolved_vintage
        self._payload = _matrix(self.vintage)
        self._acc_plus = self._resolve_acc_plus()
        self._holidays = frozenset(
            date.fromisoformat(d)
            for dates in self._payload.get("holidays", {}).values()
            for d in dates
        )

    @property
    def acc_plus(self) -> float:
        """The ACC Plus adder in $/kWh, already included in ``price_at`` totals."""
        return self._acc_plus

    @property
    def covered_years(self) -> tuple[int, int]:
        years = self._payload["years"]
        return years[0], years[-1]

    @property
    def exact_through(self) -> int:
        """Last year verified to reproduce every upstream row exactly.

        Established by ``tools/regen_data.py`` against all 350,640 source rows,
        not asserted by hand.
        """
        return int(self._payload["exact_through"])

    def _resolve_acc_plus(self) -> float:
        """The ACC Plus adder, fixed for the whole nine-year lock.

        Keyed by interconnection-application year, not by the year being priced:
        the published step-down applies to later applicants, not to an existing
        customer over time.
        """
        segment = self.config.acc_plus_segment
        if segment == "none":
            return 0.0
        year = self.config.interconnection_year
        if year is None:
            return 0.0
        # Resolved against the last day of the interconnection year, not its
        # first. The adder locks at interconnection, so what strictly governs is
        # the table in force on that *date* -- which the config does not carry,
        # only the year. Year-end is the reading that works: the first NBT table
        # took force on 2023-04-15, part-way into the first year it prices, so
        # asking for 2023-01-01 would raise for every 2023 interconnection.
        #
        # The cost is that a table revised mid-year would be applied to everyone
        # who interconnected that year, including applicants who preceded the
        # revision. No such revision has happened; if one does, this needs a real
        # interconnection date rather than a different guess at which end of the
        # year to ask for.
        table = _acc_plus_table(self.config.utility, date(year, 12, 31)).get(segment)
        if table is None:
            raise ConfigError(f"unknown acc_plus_segment {segment!r}")
        value = table.get(str(year))
        if value is None:
            raise ConfigError(
                f"no ACC Plus rate vendored for {segment} {year}; available years: {sorted(table)}"
            )
        return float(value)

    def is_locked(self, moment: datetime) -> bool:
        """Whether the rate for ``moment`` is guaranteed by the nine-year lock.

        Beyond it, PG&E still publishes values but labels them illustrative.
        """
        lock_end = self.config.lock_end
        if lock_end is None:
            # Floating customers have no lock; only the current and next
            # calendar year are effective rates upstream.
            return False
        return moment.date() <= lock_end

    def price_at(self, moment: datetime) -> ExportPrice:
        """Look up the export credit for the hour containing ``moment``.

        ``moment`` must already be in Pacific time; the engine handles that.
        """
        year = str(moment.year)
        data = self._payload["data"].get(year)
        if data is None:
            low, high = self.covered_years
            raise OutOfRangeError(
                f"{self.vintage}: no export rates for {year}; vendored data covers {low}-{high}"
            )

        kind = day_type(moment, self._holidays)
        month_index = moment.month - 1
        day_index = 0 if kind is DayType.WEEKDAY else 1
        hour = export_hour(moment)

        delivery = float(data["delivery"][month_index][day_index][hour])
        components: dict[str, float] = {"delivery": delivery}
        complete = True

        if self.config.supplier is Supplier.BUNDLED:
            components["generation"] = float(data["generation"][month_index][day_index][hour])
        else:
            # The file's generation component applies only to PG&E-bundled
            # customers. A CCA customer's generation credit comes from the CCA,
            # which this package does not ship rates for.
            cca = self.config.cca
            assert cca is not None
            if cca.export_generation_rate is not None:
                components["cca_generation"] = cca.export_generation_rate
            elif cca.rate_card is not None:
                # The CCA pays the generation half. Their tariffs tend to say
                # only that exports earn "the applicable Energy Export Credit
                # Value", so whether that equals the ACC generation component
                # used here is a per-provider question. The rate card answers it
                # via export_credit_verified, which drives `complete` below;
                # MCE's is reconciled against a real cycle, others are estimates.
                from ..cca import load_rate_card

                card = load_rate_card(cca.rate_card, moment.date())
                components["cca_generation"] = float(
                    data["generation"][month_index][day_index][hour]
                )
                if card.solar_bonus_fraction:
                    components["cca_solar_bonus"] = (
                        components["cca_generation"] * card.solar_bonus_fraction
                    )
                complete = card.export_credit_verified
            else:
                complete = False

        if self._acc_plus:
            components["acc_plus"] = self._acc_plus

        return ExportPrice(
            total=round(sum(components.values()), 6),
            vintage=self.vintage,
            day_type=kind,
            components={k: round(v, 6) for k, v in components.items()},
            locked=self.is_locked(moment),
            complete=complete,
            exact=moment.year <= self.exact_through,
        )

    def month_curve(self, year: int, month: str, kind: DayType) -> tuple[float, ...]:
        """The 24 hourly totals for one month and day type -- handy for plots."""
        data = self._payload["data"].get(str(year))
        if data is None:
            low, high = self.covered_years
            raise OutOfRangeError(f"{self.vintage}: {year} outside {low}-{high}")
        month_index = MONTHS.index(month)
        day_index = 0 if kind is DayType.WEEKDAY else 1
        generation = data["generation"][month_index][day_index]
        delivery = data["delivery"][month_index][day_index]
        include_generation = self.config.supplier is Supplier.BUNDLED
        return tuple(
            round((generation[h] if include_generation else 0.0) + delivery[h] + self._acc_plus, 6)
            for h in range(24)
        )


__all__ = ["FLOATING_VINTAGE", "NbtExportRates"]
