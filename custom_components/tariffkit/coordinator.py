"""Coordinator data and refresh logic for the Home Assistant integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from tariffkit.account import AccountError, AccountProfile, AccountRateEngine
from tariffkit.billing import Bill, BillingPeriod, CreditBalances, IntervalReading
from tariffkit.components import ComponentGroup
from tariffkit.config import CcaConfig, Config, stored_bsc_tier
from tariffkit.errors import TariffKitError
from tariffkit.interop import predbat_group_payload, predbat_payload
from tariffkit.interop.predbat import GroupPayload, PredbatPayload
from tariffkit.models import PricePoint, Supplier
from tariffkit.timeutil import hour_floor, now_pacific

from .backfill import build
from .bank import BankState, fold
from .const import (
    CONF_ACC_PLUS_SEGMENT,
    CONF_BASELINE_CODE,
    CONF_BASELINE_TERRITORY,
    CONF_BSC_TIER,
    CONF_CCA_EXPORT_RATE,
    CONF_CCA_FRANCHISE_FEE,
    CONF_CCA_GENERATION_RATES,
    CONF_CCA_NAME,
    CONF_CCA_OPTION,
    CONF_CCA_PCIA_RATE,
    CONF_CCA_PCIA_VINTAGE,
    CONF_CCA_RATE_CARD,
    CONF_DISCOUNT,
    CONF_FORECAST_HOURS,
    CONF_INTERCONNECTION_YEAR,
    CONF_MEDICAL_BASELINE,
    CONF_MEDICAL_KWH_PER_DAY,
    CONF_NSC_RATE,
    CONF_PREDBAT_ENABLED,
    CONF_PROFILE,
    CONF_PTO_DATE,
    CONF_SUPPLIER,
    CONF_TARIFF,
    CONF_VINTAGE,
    DEFAULT_FORECAST_HOURS,
    DEFAULT_PREDBAT_ENABLED,
    DOMAIN,
)
from .energy import (
    MeteredUsage,
    MeterSettings,
    UsageReader,
    coverage_warnings,
    price,
    resolve_cycle,
    statement_periods,
)
from .profile import profile_from_entry

_LOGGER = logging.getLogger(__name__)

JSONValue = str | int | float | bool | None
Provenance = dict[str, object]


@dataclass(frozen=True, slots=True)
class TariffKitQuality:
    """Quality guardrails that consumers must preserve across a rate horizon."""

    complete: bool
    exact: bool
    locked: bool

    @classmethod
    def from_point(cls, point: PricePoint) -> TariffKitQuality:
        return cls(
            complete=point.import_price.complete and point.export_price.complete,
            exact=point.export_price.exact,
            locked=point.export_price.locked,
        )

    @classmethod
    def from_points(cls, points: tuple[PricePoint, ...]) -> TariffKitQuality:
        if not points:
            raise ValueError("cannot aggregate quality for an empty rate horizon")
        qualities = tuple(cls.from_point(point) for point in points)
        return cls(
            complete=all(quality.complete for quality in qualities),
            exact=all(quality.exact for quality in qualities),
            locked=all(quality.locked for quality in qualities),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "complete": self.complete,
            "exact": self.exact,
            "locked": self.locked,
        }


def _group_values(grouped: dict[ComponentGroup, float]) -> dict[str, float]:
    """Group enum keys as plain strings, so the mapping is JSON-serializable."""
    return {str(group): value for group, value in grouped.items()}


@dataclass(frozen=True, slots=True)
class TariffKitForecastPoint:
    """The compact representation used by the chart entity.

    Carries the grouped component breakdown rather than the tariff's own
    per-line one: five or six numbers an hour that a stacked chart can draw
    directly, instead of the fifteen-odd lines behind them. The per-line detail
    stays on the current-hour price entities, where there is one hour of it
    rather than a whole horizon.
    """

    start: datetime
    end: datetime
    import_price: float
    export_price: float
    spread: float
    import_components: dict[str, float]
    export_components: dict[str, float]

    @classmethod
    def from_point(cls, point: PricePoint) -> TariffKitForecastPoint:
        return cls(
            start=point.start,
            end=point.end,
            import_price=point.import_price.total,
            export_price=point.export_price.total,
            spread=round(point.spread, 6),
            import_components=_group_values(point.import_price.grouped()),
            export_components=_group_values(point.export_price.grouped()),
        )

    def to_dict(self) -> dict[str, JSONValue | dict[str, float]]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "import": self.import_price,
            "export": self.export_price,
            "spread": self.spread,
            "import_components": dict(self.import_components),
            "export_components": dict(self.export_components),
        }


@dataclass(frozen=True, slots=True)
class TariffKitUsage:
    """What the meters have moved so far, and what it costs.

    Two bills over the same readings: the day, which is what a dashboard tile
    wants, and the cycle to date, which is what the next statement is heading
    towards. Both come from the billing engine rather than from a running
    multiplication, so a tile and a reconciled statement agree by construction.

    Note what a cycle-to-date total is not: export credits under Net Billing
    carry from one cycle to the next and settle at the annual true-up, so this
    is the cycle's own charges and credits, not a balance owed. The ledger in
    :mod:`tariffkit.billing.ledger` is where carryover lives.
    """

    metered: MeteredUsage
    #: The cycle priced through yesterday, or None when today opened the cycle
    #: and there is nothing before it. Today's own figures are this subtracted
    #: from ``cycle``, done by whoever reads a figure.
    through_yesterday: Bill | None
    cycle: Bill | None
    #: True when today is the cycle's first day, so there is legitimately
    #: nothing before it. Distinct from ``through_yesterday`` being None because
    #: the earlier days could not be priced, which is a refusal, not a zero.
    opens_today: bool = False
    #: Today priced as a period of its own. Not where today's figures come from
    #: -- the difference above is, because a baseline allowance accrues over the
    #: cycle rather than the day -- but it is the only library-computed
    #: time-of-use split a single day has, and the only figure left when the
    #: cycle cannot be priced at all.
    day: Bill | None = None
    #: Why a span has no bill, empty when it has one. An entity that reads
    #: `unknown` has to be able to say what stopped it.
    today_reason: str = ""
    cycle_reason: str = ""
    #: Gaps, overlaps and reconstructed intervals in the metered series. Real
    #: problems, unlike the "the day is not over yet" shortfall the engine's own
    #: coverage check would also report.
    coverage: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """False when a bill is missing, unpriced, or the meters are silent."""
        if self.cycle is None:
            return False
        if self.through_yesterday is None and not self.opens_today:
            # The earlier days were refused, so today has no figure at all.
            return False
        return (
            (self.through_yesterday is None or self.through_yesterday.complete)
            and self.cycle.complete
            and not self.metered.missing
            and not self.metered.dropped
            and not self.coverage
        )

    def warnings(self, span: str = "cycle") -> tuple[str, ...]:
        """Why a span's figure may be wrong, from the meters and from pricing."""
        found: list[str] = [
            f"no recorder statistics for {entity}" for entity in self.metered.missing
        ]
        if self.metered.dropped:
            found.append(
                f"{self.metered.dropped} hour(s) were discarded as implausible, so "
                f"their energy is missing from these totals"
            )
        found.extend(self.coverage)
        # Today's figures are read off the cycle, so the cycle's own pricing
        # warnings are today's too. Only where the cycle is unpriceable does
        # today stand alone, and then the standalone bill carries them.
        bill = (self.cycle or self.day) if span == "today" else self.cycle
        reason = self.today_reason if span == "today" else self.cycle_reason
        if reason:
            found.append(reason)
        if bill is not None:
            found.extend(bill.warnings)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class TariffKitData:
    """All state needed by entities, diagnostics, and response actions."""

    point: PricePoint
    forecast: tuple[TariffKitForecastPoint, ...]
    quality: TariffKitQuality
    provenance: Provenance
    #: Base Services Charge in USD/day. Kept beside the per-kWh prices rather
    #: than inside them: it is a fixed daily amount, so adding it to a marginal
    #: price would misprice every kWh.
    daily_fixed_charge: float
    generated_at: datetime = field(compare=False)
    predbat: PredbatPayload | None = None
    predbat_warning: str | None = None
    #: Two-day forecast curve per direction and component group, for stacked
    #: dashboard charts. Unlike ``predbat`` this is not gated on Predbat mode --
    #: charting what the price is made of has nothing to do with Predbat.
    curves: GroupPayload | None = None
    #: None whenever no meter entities are configured, which is the default.
    usage: TariffKitUsage | None = None
    #: Why ``usage`` is absent despite meters being configured. Empty when
    #: there is nothing to explain.
    usage_note: str = ""
    #: The export credit bank, recomputed when a cycle closes rather than on
    #: every tick. None when there is no PTO, no meters, or nothing priced.
    bank: BankState | None = None
    #: Set when a bank ought to exist but does not yet, with the reason. None
    #: when no bank is expected at all, which needs no explanation.
    bank_pending: str | None = None

    @property
    def opening(self) -> CreditBalances | None:
        """The bank a cycle's charges may be offset against, or None.

        What is actually owed depends on the credit carried in, so this decides
        a headline dollar figure. It is therefore refused rather than guessed in
        three cases, each of which used to pass silently:

        A bank that is not trustworthy. ``BankState`` already refuses to vouch
        for a balance folded across a gap or a supplier change, and the bank
        entity prints the reason -- but the money entities took the number
        anyway, halving a $348 cycle to $156 while reporting themselves
        complete.

        A bank not folded yet. The first refresh deliberately skips the fold, so
        for one tick after every restart there is no balance. Using none is
        right; doing it silently is not, because the figure then changes by the
        whole bank a minute later with no usage behind it.

        No bank at all -- no Permission To Operate, or the first cycle. Nothing
        has been earned, so nothing carries, and there is no caveat to make.
        """
        if self.bank is None or not self.bank.trustworthy:
            return None
        return self.bank.balance

    @property
    def opening_note(self) -> str:
        """Why the figures were computed without a bank, or empty."""
        if self.usage is None or self.bank_pending is None:
            return ""
        if self.bank is None:
            return self.bank_pending
        if not self.bank.trustworthy:
            return (
                f"the export credit bank is not trustworthy ({'; '.join(self.bank.warnings)}), "
                f"so no carried credit was applied and these charges are stated before any "
                f"bank offsets them"
            )
        return ""

    def chart_attributes(self) -> dict[str, object]:
        return {
            "rates": [rate.to_dict() for rate in self.forecast],
            "quality": self.quality.to_dict(),
            "generated_at": self.generated_at.isoformat(),
        }


def config_from_entry(data: dict[str, Any]) -> Config:
    """Translate a legacy flat config entry into a library Config."""
    if CONF_PROFILE in data:
        return profile_from_entry(data).config_at(now_pacific())
    supplier = Supplier(data.get(CONF_SUPPLIER, "bundled"))
    cca = None
    if supplier is Supplier.CCA:
        cca = CcaConfig(
            name=data.get(CONF_CCA_NAME, ""),
            rate_card=data.get(CONF_CCA_RATE_CARD) or None,
            option=data.get(CONF_CCA_OPTION, "light_green"),
            pcia_vintage=data.get(CONF_CCA_PCIA_VINTAGE),
            pcia_rate=data.get(CONF_CCA_PCIA_RATE),
            franchise_fee_surcharge=data.get(CONF_CCA_FRANCHISE_FEE),
            generation_rates=data.get(CONF_CCA_GENERATION_RATES, {}),
            export_generation_rate=data.get(CONF_CCA_EXPORT_RATE),
        )
    raw_year = data.get(CONF_INTERCONNECTION_YEAR)
    if raw_year in (None, ""):
        interconnection_year = None
    else:
        try:
            interconnection_year = int(raw_year)
        except (TypeError, ValueError) as err:
            raise TariffKitError("interconnection_year must be a year") from err
    raw_pto = data.get(CONF_PTO_DATE)
    if raw_pto in (None, ""):
        pto = None
    elif isinstance(raw_pto, str):
        try:
            pto = date.fromisoformat(raw_pto)
        except ValueError as err:
            raise TariffKitError("pto_date must be an ISO date") from err
    else:
        pto = raw_pto
    return Config(
        supplier=supplier,
        tariff=data.get(CONF_TARIFF, "E-ELEC"),
        interconnection_year=interconnection_year,
        pto_date=pto,
        vintage=data.get(CONF_VINTAGE) or None,
        acc_plus_segment=data.get(CONF_ACC_PLUS_SEGMENT, "residential"),
        discount=data.get(CONF_DISCOUNT, "none"),
        # Through the shared reader, not straight from the entry. This path
        # does not go via `Config.from_dict`, so the healing that lets a
        # profile stored before the tier followed the discount take its
        # programme's tier has to be applied here too -- without it every
        # existing CARE entry stayed on the tier 3 it was serialized with,
        # which is the overcharge this was meant to end.
        base_services_charge_tier=stored_bsc_tier(
            data.get(CONF_BSC_TIER), data.get(CONF_DISCOUNT, "none")
        ),
        baseline_territory=data.get(CONF_BASELINE_TERRITORY) or None,
        baseline_code=data.get(CONF_BASELINE_CODE, "basic"),
        medical_baseline=bool(data.get(CONF_MEDICAL_BASELINE, False)),
        medical_kwh_per_day=(
            float(data[CONF_MEDICAL_KWH_PER_DAY])
            if data.get(CONF_MEDICAL_KWH_PER_DAY) is not None
            else None
        ),
        cca=cca,
        nsc_rate=data.get(CONF_NSC_RATE),
    )


def device_model(info: Mapping[str, Any]) -> str:
    """The device's rate identity: tariff, export vintage, and any CCA."""
    tariff = info.get("tariff")
    model = tariff if isinstance(tariff, str) and tariff else "unknown"
    vintage = info.get("export_vintage")
    if vintage:
        model = f"{model} / {vintage}"
    # On a CCA account the utility still delivers, so it stays the manufacturer
    # and the generation supplier joins the rate identity here.
    generation = _generation_supplier(info)
    if generation:
        model = f"{model} · {generation}"
    return model


def _generation_supplier(info: Mapping[str, Any]) -> str | None:
    """Name the CCA supplying generation, with its product tier when known.

    Returns None for bundled accounts, where PG&E supplies generation too and
    naming it again on the model line would say nothing.
    """
    if str(info.get("supplier")) != str(Supplier.CCA):
        return None
    name = info.get("cca_name") or info.get("cca_rate_card")
    if not isinstance(name, str) or not name:
        return None
    name = name if info.get("cca_name") else name.upper()
    option = info.get("cca_option")
    if isinstance(option, str) and option:
        return f"{name} {option.replace('_', ' ').title()}"
    return name


class TariffKitCoordinator(DataUpdateCoordinator[TariffKitData]):
    """Recompute current and forecast rates at a short boundary-safe interval."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self._device_model: str | None = None
        self.profile: AccountProfile = profile_from_entry({**entry.data, **entry.options})
        self.engine = AccountRateEngine(self.profile)
        self.meters = MeterSettings.from_entry({**entry.data, **entry.options}, self.profile)
        self._usage = (
            UsageReader(hass, self.meters, statement_periods(self.profile))
            if self.meters.configured
            else None
        )
        self.forecast_hours = int(
            entry.options.get(
                CONF_FORECAST_HOURS,
                entry.data.get(CONF_FORECAST_HOURS, DEFAULT_FORECAST_HOURS),
            )
        )
        if not 1 <= self.forecast_hours <= 168:
            raise TariffKitError("forecast_hours must be between 1 and 168")
        self.predbat_enabled = bool(
            entry.options.get(
                CONF_PREDBAT_ENABLED,
                entry.data.get(CONF_PREDBAT_ENABLED, DEFAULT_PREDBAT_ENABLED),
            )
        )
        self._usage_note = ""
        self._bank_key: tuple[object, ...] | None = None
        self._settled_once = False
        self._bank_failed = False
        self._bank: BankState | None = None
        self._bank_note: str | None = None
        self._predbat_key: tuple[date, date] | None = None
        self._predbat: PredbatPayload | None = None
        self._curves_key: tuple[date, date] | None = None
        self._curves: GroupPayload | None = None
        self._predbat_warning: str | None = None
        if self.predbat_enabled and hass.config.time_zone != "America/Los_Angeles":
            self._predbat_warning = (
                "Predbat expects Home Assistant's time zone to be America/Los_Angeles"
            )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=1),
            always_update=False,
            config_entry=entry,
        )

    async def _async_update_data(self) -> TariffKitData:
        metered = await self._async_metered()
        # Inside the guard, and catching more than the rate path needs to. The
        # bank reaches into the ledger and the true-up tables, which can raise
        # for reasons that have nothing to do with whether prices can be
        # computed -- and an optional feature must not cost the rate entities
        # their value, which is the same rule `_async_metered` follows.
        try:
            bank, bank_pending = await self._async_bank(metered)
        except (TariffKitError, HomeAssistantError, ValueError, ArithmeticError) as err:
            _LOGGER.warning("Unable to compute the export credit bank: %s", err)
            bank = None
            bank_pending = (
                f"the export credit bank could not be folded ({err}), so these charges are "
                f"stated before any bank offsets them"
            )
        try:
            data = await self.hass.async_add_executor_job(
                self._compute, metered, bank, bank_pending
            )
        except TariffKitError as err:
            raise UpdateFailed(str(err)) from err
        self._sync_device_identity(data.provenance)
        return data

    async def _async_bank(
        self, metered: MeteredUsage | None
    ) -> tuple[BankState | None, str | None]:
        """The credit bank, refreshed only when the billing cycle turns over.

        Folding it means pricing every cycle since Permission To Operate, which
        is seconds of work and a months-long recorder read -- far too much for a
        one-minute tick, and pointless at that rate. A bank only moves when a
        cycle closes and its credits are applied, so the cycle's own start is
        the cache key.

        Returns the bank and, where one is expected but absent, why. The reason
        travels because the bank decides what a cycle actually owes: an entity
        computing without one has to say so, or it publishes a figure that moves
        by the whole balance on the next tick with no usage behind it.
        """
        if metered is None or self._usage is None:
            return None, None
        if not self._settled_once:
            # Not on the first refresh. That one is awaited inside
            # `async_setup_entry`, and folding a long history there costs
            # seconds -- measured at eleven for five years -- which shows up as
            # Home Assistant's slow-setup warning and delays every other entity.
            # A minute later costs nothing and nobody is watching.
            self._settled_once = True
            return None, (
                "the export credit bank is folded on the first tick after startup and has "
                "not been yet, so these charges are stated before any bank offsets them"
            )
        try:
            pto = self.profile.config_at(now_pacific()).pto_date
        except AccountError:
            return None, None
        if pto is None:
            # Without Permission To Operate nothing is compensated, so there is
            # no bank to carry -- not an empty one, none.
            return None, None
        opens = max(
            min(
                resolve_cycle(
                    pto, self.meters.cycle_start_day, statement_periods(self.profile)
                ).start,
                metered.cycle.start,
            ),
            min(self.profile.effective_dates),
        )
        closes = metered.cycle.start - timedelta(days=1)
        # A cycle only closes once, so its start is the natural cache key -- but
        # the fold would then happen on the first tick after midnight, when the
        # cycle's final hour has not been compiled yet. The recorder writes the
        # hourly row for 23:00 at about 00:00:10, and this ticks every minute on
        # a fixed second. A fold that lands in that window prices the last cycle
        # short of an hour and, cached on the cycle alone, would stay wrong for a
        # month. So an untrustworthy fold is retried, keyed on the hour, for the
        # first day of a new cycle -- long enough to outlast the compile, short
        # enough that a genuine permanent gap is not re-read every hour forever.
        settling = (now_pacific().date() - metered.cycle.start).days < 1
        unsettled = self._bank is not None and not self._bank.trustworthy
        # One shape, always. A key built differently on the error path than on
        # the success path can never match the next tick's, so the guard never
        # fires and the read it was meant to throttle happens every minute.
        retry = (
            hour_floor(now_pacific()) if (self._bank_failed or (settling and unsettled)) else None
        )
        key: tuple[object, ...] = (opens, metered.cycle.start, retry)
        if key == self._bank_key:
            return self._bank, self._bank_note
        if closes < opens:
            # The first cycle has not closed yet, so nothing has banked.
            self._bank_key, self._bank = key, None
            self._bank_note = None
            return None, None
        try:
            readings = await self._usage.async_readings(opens, closes)
        except (HomeAssistantError, ValueError) as err:
            # Keyed on the hour, so a recorder that stays broken is retried
            # hourly rather than being asked for months of statistics every
            # sixty seconds for as long as it stays broken.
            _LOGGER.warning("Unable to read history for the credit bank: %s", err)
            self._bank_failed = True
            self._bank_key = (opens, metered.cycle.start, hour_floor(now_pacific()))
            self._bank_note = (
                f"the export credit bank could not be read from the recorder ({err}), so "
                f"these charges are stated before any bank offsets them"
            )
            return self._bank, self._bank_note
        self._bank = await self.hass.async_add_executor_job(
            self._fold_bank, readings, opens, closes
        )
        self._bank_failed = False
        self._bank_key = key
        self._bank_note = (
            None
            if self._bank is not None
            else "no priced cycle closed before this one, so nothing has banked yet"
        )
        return self._bank, self._bank_note

    def _fold_bank(
        self, readings: list[IntervalReading], opens: date, closes: date
    ) -> BankState | None:
        """Price every closed cycle in the span and fold them.

        Through `backfill.build` rather than by pricing cycles here, so the bank
        inherits every guard the backfill grew: a window clipped to the evidence,
        days the recorder cannot account for left unpriced, a cycle joined
        partway through refused outright. Pricing cycles directly would fold a
        bank out of months the meters say nothing about -- an empty cycle still
        has a Base Services Charge, so it prices to something rather than to
        nothing, and a balance of zero built from fabricated cycles looks exactly
        like a balance of zero.
        """
        result = build(self.profile, readings, opens, closes, self.meters.cycle_start_day)
        if not result.bills:
            return None
        state = fold(self.profile, result.bills)
        # `uncovered` is the only thing that notices an hour missing from the
        # *end* of the window: a trailing hole is not a gap between readings, so
        # the coverage check cannot see it, and the day it belongs to still has
        # its other twenty-three hours and prices without complaint. It lives on
        # the reader and was previously surfaced only in the backfill action's
        # response, which left this path silent about exactly the case that
        # recurs every time a cycle rolls over.
        uncovered = () if self._usage is None else self._usage.absent
        if result.unpriced:
            # The cycle bills carry these days' energy even though the daily
            # rows refused to publish it, and their time-of-use split is a
            # guess -- which is exactly what the bank is folding.
            uncovered = (
                *uncovered,
                f"{len(result.unpriced)} day(s) inside the folded cycles hold an hour "
                f"reconstructed across an outage, so those cycles' time-of-use split is "
                f"a guess even though their energy is not",
            )
        extra = tuple(
            w for w in (*result.skipped, *result.warnings, *uncovered) if w not in state.warnings
        )
        return replace(state, warnings=(*state.warnings, *extra)) if extra else state

    async def _async_metered(self) -> MeteredUsage | None:
        """Read the meters, or nothing if they are unconfigured or unreadable.

        A recorder that cannot answer costs the usage entities their value for
        this tick; it must not cost the rate entities theirs, which is why this
        degrades to None rather than failing the update.
        """
        if self._usage is None:
            return None
        if "recorder" not in self.hass.config.components:
            self._usage_note = (
                "Home Assistant's recorder is not enabled, so there are no "
                "statistics to read the meters from"
            )
            return None
        try:
            metered = await self._usage.async_usage(now_pacific())
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Unable to read metered energy: %s", err)
            self._usage_note = f"could not read metered energy: {err}"
            return None
        self._usage_note = ""
        return metered

    def _sync_device_identity(self, provenance: Provenance) -> None:
        """Keep the device's rate identity current as the profile changes epoch.

        DeviceInfo is read once, when the platform registers the entities, so an
        account that crosses into an epoch on a different tariff, export vintage
        or generation supplier would otherwise keep showing the old identity
        until the entry is reloaded.
        """
        model = device_model(provenance)
        if model == self._device_model:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.entry.entry_id)})
        if device is not None:
            registry.async_update_device(device.id, model=model)
        self._device_model = model

    def _day_key(self, moment: datetime) -> tuple[date, date]:
        """Day plus the rate vintage in force, so a mid-day change still rebuilds."""
        active_effective = max(
            effective for effective in self.profile.effective_dates if effective <= moment.date()
        )
        return (moment.date(), active_effective)

    def _predbat_for(self, moment: datetime) -> PredbatPayload | None:
        if not self.predbat_enabled:
            return None
        key = self._day_key(moment)
        if key != self._predbat_key:
            self._predbat = predbat_payload(self.engine, moment)
            self._predbat_key = key
        return self._predbat

    def _curves_for(self, moment: datetime) -> GroupPayload:
        """Rebuilt once a day rather than every tick -- the curve only moves at midnight."""
        key = self._day_key(moment)
        curves = self._curves
        if curves is None or key != self._curves_key:
            curves = predbat_group_payload(self.engine, moment)
            self._curves, self._curves_key = curves, key
        return curves

    def _compute(
        self,
        metered: MeteredUsage | None = None,
        bank: BankState | None = None,
        bank_pending: str | None = None,
    ) -> TariffKitData:
        point = self.engine.price_now()
        curve = self.engine.forecast(self.forecast_hours, start=point.start)
        curve_points = tuple(curve.points)
        raw_provenance = self.engine.describe(point.start)
        provenance: Provenance = {
            key: raw_provenance[key]
            for key in (
                "utility",
                "tariff",
                "supplier",
                "cca_name",
                "cca_rate_card",
                "cca_option",
                "tariff_effective",
                "tariff_advice_letter",
                "tariff_source",
                "export_vintage",
                "export_years",
                "acc_plus",
                "pto_date",
                "lock_end",
                "account_profile",
            )
            if key in raw_provenance
        }
        return TariffKitData(
            point=point,
            forecast=tuple(TariffKitForecastPoint.from_point(rate) for rate in curve_points),
            quality=TariffKitQuality.from_points(curve_points),
            provenance=provenance,
            daily_fixed_charge=self.engine.daily_fixed_charge(point.start),
            # Keep generated time out of equality so minute polling does not
            # fan out unchanged entity states and websocket attributes.
            generated_at=now_pacific(),
            predbat=self._predbat_for(point.start),
            predbat_warning=self._predbat_warning,
            curves=self._curves_for(point.start),
            usage=self._usage_for(metered),
            usage_note=self._usage_note,
            bank=bank,
            bank_pending=bank_pending,
        )

    @property
    def split_supply(self) -> bool:
        """True when a Community Choice Aggregator supplies generation.

        Which is what makes the export credit two banks rather than one, so it
        decides how many bank entities exist. Read once at setup: a supplier
        change is an account-history edit, and that reloads the entry.
        """
        try:
            return self.profile.config_at(now_pacific()).supplier is Supplier.CCA
        except AccountError:
            return False

    async def async_history(self, opens: date, closes: date) -> list[IntervalReading]:
        """Hourly metered readings across a past window, for backfilling.

        Separate from the coordinator's own cached read, which only ever covers
        the running cycle: this asks for a span that may be months long and
        should not disturb what the live entities are working from.
        """
        if self._usage is None:
            return []
        return await self._usage.async_readings(opens, closes)

    @property
    def discarded_history(self) -> tuple[date, ...]:
        """Days the last history read had to drop an implausible hour on."""
        return () if self._usage is None else self._usage.discarded

    @property
    def uncovered_meters(self) -> tuple[str, ...]:
        """Configured meters the last history read could not fully cover."""
        return () if self._usage is None else self._usage.absent

    def _usage_for(self, metered: MeteredUsage | None) -> TariffKitUsage | None:
        """Price the day and the cycle to date over the same readings."""
        if metered is None:
            return None
        cycle, cycle_reason = price(self.profile, metered.readings, metered.cycle)
        opens_today = metered.today.start <= metered.cycle.start
        earlier, earlier_reason = self._through_yesterday(metered)
        day, day_reason = price(self.profile, metered.for_today(), metered.today)
        return TariffKitUsage(
            metered=metered,
            through_yesterday=earlier,
            cycle=cycle,
            opens_today=opens_today,
            day=day,
            today_reason=self._today_reason(cycle, day, cycle_reason, earlier_reason, day_reason),
            cycle_reason=cycle_reason,
            coverage=coverage_warnings(metered.readings, metered.cycle),
        )

    def _through_yesterday(self, metered: MeteredUsage) -> tuple[Bill | None, str]:
        """The cycle priced through *yesterday*, for the entities to difference.

        Parts of a bill are cumulative over a cycle rather than additive over
        its days -- the baseline allowance most of all -- so pricing today alone
        overstates a heavy day. Two cycle-to-date bills have neither problem.

        Only the two bills are produced here. Subtracting one figure from
        another happens where the figure is read, on values the library
        computed: rebuilding a whole ``Bill`` bucket by bucket was an arithmetic
        of this integration's own invention standing in front of an engine that
        reconciles against printed statements, which is where drift begins.

        None is the answer on the cycle's first day, and it is not a failure:
        nothing precedes today, so today *is* the cycle to date and there is
        nothing to subtract.
        """
        opened = metered.cycle.start
        today = metered.today.start
        if today <= opened:
            return None, ""
        period = BillingPeriod(opened, today - timedelta(days=1))
        readings = [r for r in metered.readings if period.contains(r.start)]
        return price(self.profile, readings, period)

    @staticmethod
    def _today_reason(
        cycle: Bill | None,
        day: Bill | None,
        cycle_reason: str,
        earlier_reason: str,
        day_reason: str,
    ) -> str:
        """Why today has no figure, or the caveat on the one it has.

        Today is normally the difference of two cycle-to-date bills, so either
        of them failing takes today with it. Where the cycle cannot be priced at
        all -- an account history that begins mid-cycle -- the standalone day
        bill is still exact on any schedule without a baseline allowance, which
        is most of them, so give the number and name the caveat rather than
        withholding a figure that is usually right.
        """
        if cycle is not None:
            return earlier_reason
        if day is None:
            return day_reason or cycle_reason
        return (
            f"{cycle_reason}; today is priced on its own instead. On a schedule with a "
            f"baseline allowance that grants one day's allowance rather than the "
            f"cycle's, which can overstate a heavy day"
        )

    @property
    def current_hour(self) -> datetime:
        point: PricePoint = self.data.point
        return point.start


type TariffKitConfigEntry = ConfigEntry[TariffKitCoordinator]
