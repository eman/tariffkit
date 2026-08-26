"""Response-returning Home Assistant actions for TariffKit."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector, service
from homeassistant.util.json import JsonObjectType, JsonValueType

from tariffkit.account import AccountProfile
from tariffkit.errors import TariffKitError
from tariffkit.interop import forecast_lists, resample
from tariffkit.models import PriceCurve, PricePoint
from tariffkit.timeutil import PACIFIC, hour_floor, now_pacific, to_pacific

from . import backfill
from .const import (
    CONF_CONFIG_ENTRY,
    CONF_DATE,
    CONF_END,
    CONF_HORIZON,
    CONF_RESOLUTION,
    CONF_START,
    DOMAIN,
    SERVICE_BACKFILL_USAGE,
    SERVICE_GET_EMHASS_FORECAST,
    SERVICE_GET_RATES,
    SUPPORTED_RESOLUTIONS,
)
from .coordinator import TariffKitCoordinator, TariffKitQuality
from .energy import resolve_cycle, statement_periods

MAX_HOURS = 168

_COMMON_SCHEMA = {
    vol.Required(CONF_CONFIG_ENTRY): selector.ConfigEntrySelector(
        selector.ConfigEntrySelectorConfig(integration=DOMAIN)
    ),
    vol.Optional(CONF_START): vol.Any(str, datetime),
    vol.Optional(CONF_END): vol.Any(str, datetime),
    vol.Optional(CONF_DATE): vol.Any(str, date),
    vol.Optional(CONF_HORIZON): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_HOURS)),
}
_RATES_SCHEMA = vol.Schema(
    {
        **_COMMON_SCHEMA,
        vol.Optional(CONF_RESOLUTION, default=60): vol.All(
            vol.Coerce(int), vol.In(SUPPORTED_RESOLUTIONS)
        ),
    }
)
_EMHASS_SCHEMA = vol.Schema(
    {
        **_COMMON_SCHEMA,
        vol.Optional(CONF_RESOLUTION, default=30): vol.All(
            vol.Coerce(int), vol.In(SUPPORTED_RESOLUTIONS)
        ),
    }
)


def _timestamp(value: object, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as err:
            raise ServiceValidationError(
                f"{label} must be an ISO 8601 timestamp with an explicit offset"
            ) from err
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ServiceValidationError(
            f"{label} must be an ISO 8601 timestamp with an explicit offset"
        )
    if value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset():
        raise ServiceValidationError(f"{label} is ambiguous at a daylight-saving transition")
    return to_pacific(value)


def _calendar_date(value: object) -> date:
    if isinstance(value, datetime):
        raise ServiceValidationError("date must be an ISO calendar date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as err:
            raise ServiceValidationError("date must be an ISO calendar date") from err
    raise ServiceValidationError("date must be an ISO calendar date")


def _window(data: Mapping[str, Any]) -> tuple[datetime, datetime, int]:
    raw_start = data.get(CONF_START)
    raw_end = data.get(CONF_END)
    raw_date = data.get(CONF_DATE)
    horizon = int(data.get(CONF_HORIZON, 24))
    resolution = int(data[CONF_RESOLUTION])
    if (raw_start is not None or raw_end is not None) and raw_date is not None:
        raise ServiceValidationError("choose start/end or date, not both")
    if raw_end is not None and raw_start is None:
        raise ServiceValidationError("end requires start")

    if raw_start is not None:
        start = _timestamp(raw_start, "start")
        if raw_end is not None:
            if CONF_HORIZON in data:
                raise ServiceValidationError("horizon cannot be combined with an explicit end")
            end = _timestamp(raw_end, "end")
        else:
            end = (start.astimezone(UTC) + timedelta(hours=horizon)).astimezone(PACIFIC)
    elif raw_date is not None:
        start = datetime.combine(_calendar_date(raw_date), time.min, tzinfo=PACIFIC)
        end = (start.astimezone(UTC) + timedelta(hours=horizon)).astimezone(PACIFIC)
    else:
        start = now_pacific()
        start = start.replace(
            minute=(start.minute // resolution) * resolution,
            second=0,
            microsecond=0,
        )
        end = (start.astimezone(UTC) + timedelta(hours=horizon)).astimezone(PACIFIC)

    if start >= end:
        raise ServiceValidationError("the requested forecast window must end after it starts")
    if start.minute % resolution or start.second or start.microsecond:
        raise ServiceValidationError(f"start must align to a {resolution}-minute slot")
    if end.minute % resolution or end.second or end.microsecond:
        raise ServiceValidationError(f"end must align to a {resolution}-minute slot")
    if end.astimezone(UTC) - start.astimezone(UTC) > timedelta(hours=MAX_HOURS):
        raise ServiceValidationError(f"the forecast window cannot exceed {MAX_HOURS} hours")
    return start, end, resolution


def _slots(
    coordinator: TariffKitCoordinator,
    start: datetime,
    end: datetime,
    resolution: int,
) -> tuple[PricePoint, ...]:
    floor = hour_floor(start)
    duration = end.astimezone(UTC) - floor.astimezone(UTC)
    hours = max(1, math.ceil(duration.total_seconds() / 3600))
    try:
        curve = coordinator.engine.forecast(hours=hours, start=floor)
    except TariffKitError as err:
        raise ServiceValidationError(str(err)) from err
    slots = resample(curve, resolution)
    selected = tuple(slot for slot in slots if slot.start >= start and slot.end <= end)
    if not selected or selected[0].start != start or selected[-1].end != end:
        raise ServiceValidationError(
            "the requested window does not align with the available tariff timeline"
        )
    return selected


_PROVENANCE_KEYS = (
    "utility",
    "tariff",
    "supplier",
    # A CCA supplies generation while the utility still delivers, so a consumer
    # reading the generation component needs to know whose rate it came from.
    "cca_name",
    "cca_rate_card",
    "cca_option",
    "tariff_effective",
    "tariff_advice_letter",
    "tariff_source",
    "export_vintage",
    "export_years",
    "acc_plus",
    "lock_end",
    "account_profile",
    "account_effective",
)


def _json_value(value: object) -> JsonValueType:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RuntimeError("provenance object keys must be strings")
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    raise RuntimeError(f"non-JSON provenance value: {value!r}")


def _trim_provenance(data: Mapping[str, object]) -> JsonObjectType:
    return {key: _json_value(data[key]) for key in _PROVENANCE_KEYS if key in data}


def _provenance(coordinator: TariffKitCoordinator, slots: tuple[PricePoint, ...]) -> JsonObjectType:
    segments: list[JsonValueType] = []
    active: JsonObjectType | None = None
    segment_start = slots[0].start
    for slot in slots:
        current = _trim_provenance(coordinator.engine.describe(slot.start))
        if active is None:
            active = current
            continue
        if current == active:
            continue
        segments.append(
            {
                "start": segment_start.isoformat(),
                "end": slot.start.isoformat(),
                **active,
            }
        )
        active = current
        segment_start = slot.start
    if active is None:
        raise RuntimeError("cannot describe provenance for an empty rate window")
    segments.append(
        {
            "start": segment_start.isoformat(),
            "end": slots[-1].end.isoformat(),
            **active,
        }
    )
    return {"segments": segments}


def _quality(quality: TariffKitQuality) -> JsonObjectType:
    return {
        "complete": quality.complete,
        "exact": quality.exact,
        "locked": quality.locked,
    }


def _rates_response(
    coordinator: TariffKitCoordinator,
    start: datetime,
    end: datetime,
    resolution: int,
) -> JsonObjectType:
    slots = _slots(coordinator, start, end, resolution)
    quality = TariffKitQuality.from_points(tuple(slots))
    return {
        "start": slots[0].start.isoformat(),
        "end": slots[-1].end.isoformat(),
        "resolution": resolution,
        "points": [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "import": slot.import_price.total,
                "export": slot.export_price.total,
                "spread": round(slot.spread, 6),
                "quality": _quality(TariffKitQuality.from_point(slot)),
            }
            for slot in slots
        ],
        "quality": _quality(quality),
        "generated_at": now_pacific().isoformat(),
        "provenance": _provenance(coordinator, slots),
    }


def _emhass_response(
    coordinator: TariffKitCoordinator,
    start: datetime,
    end: datetime,
    resolution: int,
) -> JsonObjectType:
    slots = _slots(coordinator, start, end, resolution)
    values = forecast_lists(PriceCurve(tuple(slots)), minutes=resolution, since=start)
    return {
        **values,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "resolution": resolution,
        "quality": _quality(TariffKitQuality.from_points(tuple(slots))),
        "generated_at": now_pacific().isoformat(),
        "provenance": _provenance(coordinator, slots),
    }


def _coordinator(hass: HomeAssistant, entry_id: str) -> TariffKitCoordinator:
    entry = service.async_get_config_entry(hass, DOMAIN, entry_id)
    coordinator = getattr(entry, "runtime_data", None)
    if not isinstance(coordinator, TariffKitCoordinator):
        raise ServiceValidationError("the selected TariffKit entry is not loaded")
    return coordinator


async def _get_rates(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator = _coordinator(hass, call.data[CONF_CONFIG_ENTRY])
    start, end, resolution = _window(call.data)
    return _rates_response(coordinator, start, end, resolution)


async def _get_emhass_forecast(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator = _coordinator(hass, call.data[CONF_CONFIG_ENTRY])
    start, end, resolution = _window(call.data)
    return _emhass_response(coordinator, start, end, resolution)


_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY): cv.string,
        vol.Optional(CONF_START): cv.string,
    }
)


async def _backfill_usage(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Price metered history into long-term statistics.

    Recomputes the whole window every time rather than resuming where it left
    off. External statistics replace a period rather than appending to it, so a
    rerun is the supported way to pick up a corrected account history -- and a
    backfill that only ever appended would leave the days before the correction
    priced under settings the account no longer claims.
    """
    coordinator = _coordinator(hass, call.data[CONF_CONFIG_ENTRY])
    if not coordinator.meters.configured:
        raise ServiceValidationError(
            "no meter entities are configured for this account; set them under "
            "Configure -> Metered energy before backfilling"
        )
    if "recorder" not in hass.config.components:
        raise ServiceValidationError(
            "Home Assistant's recorder is not enabled, so there are no statistics to price"
        )
    profile = coordinator.profile
    if not profile.name:
        raise ServiceValidationError("backfilling needs a named account profile")

    _check_publishable(profile.name)
    opens = _backfill_start(call.data, profile, coordinator.meters.cycle_start_day)
    # Read from the containing cycle's first day, not from the day asked for. A
    # cycle can only be decomposed from its own start, so a window opening
    # partway through one would otherwise lose it entirely -- and an epoch date
    # is rarely a cycle boundary, so the default start lands mid-cycle routinely.
    periods = statement_periods(profile)
    opens = min(opens, resolve_cycle(opens, coordinator.meters.cycle_start_day, periods).start)
    closes = now_pacific().date() - timedelta(days=1)
    if opens > closes:
        raise ServiceValidationError(
            f"nothing to price: {opens} is not before today. Backfill covers whole "
            "days that have finished; the running totals cover today."
        )
    readings = await coordinator.async_history(opens, closes)
    result = await hass.async_add_executor_job(
        backfill.build, profile, readings, opens, closes, coordinator.meters.cycle_start_day
    )
    result.warnings.extend(coordinator.uncovered_meters)
    discarded = coordinator.discarded_history
    if discarded:
        # Days, not hours: the reader deduplicates by date, so a day that lost
        # several hours counts once. Saying "hours" here would understate it.
        result.warnings.append(
            f"implausible readings were discarded on {len(discarded)} day(s) "
            f"({discarded[0]}..{discarded[-1]}); that energy is missing from these "
            f"totals, so they understate what was actually used"
        )
    await backfill.async_publish(hass, profile.name, result)
    return cast(ServiceResponse, result.summary(profile.name))


def _check_publishable(profile_name: str) -> None:
    """Refuse a name no statistic id can carry, before doing any work.

    Failing this late means failing after the recorder query and the whole
    pricing pass, with a message from deep inside the recorder that names
    neither the profile nor the field.
    """
    from homeassistant.components.recorder.statistics import VALID_STATISTIC_ID

    for series in backfill.SERIES:
        statistic_id = series.statistic_id(profile_name)
        if not VALID_STATISTIC_ID.match(statistic_id):
            raise ServiceValidationError(
                f"the profile name {profile_name!r} cannot be published as a statistic "
                f"({statistic_id!r} is not a valid statistic id). Rename the profile "
                f"using only letters, digits and underscores"
            )


def _backfill_start(data: Mapping[str, Any], profile: AccountProfile, start_day: int) -> date:
    """Where to begin, defaulting to the billing cycle that contains PTO.

    That is where bills start meaning anything. Net Billing compensation runs
    from Permission To Operate, so an earlier cycle earns nothing however much
    it exported, and a credit bank folded from this point opens at zero by
    definition rather than at some balance nobody can reconstruct.

    The cycle *containing* PTO, not PTO itself: a cycle can only be decomposed
    from its own first day, and PTO falls mid-cycle far more often than not. Its
    pre-PTO days are priced correctly for what they are -- imported energy
    charged, exported energy earning nothing, which the engine says out loud.

    An account with no PTO has no such landmark, so it falls back to the
    profile's first epoch, which is the earliest date anything can be priced
    under at all.
    """
    raw = data.get(CONF_START)
    if raw in (None, ""):
        pto = profile.config_at(now_pacific()).pto_date
        if pto is None:
            return min(profile.effective_dates)
        periods = statement_periods(profile)
        return max(resolve_cycle(pto, start_day, periods).start, min(profile.effective_dates))
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as err:
        raise ServiceValidationError(f"start must be an ISO date, got {raw!r}") from err


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register response actions once for the integration."""
    if not hass.services.has_service(DOMAIN, SERVICE_GET_RATES):

        async def handle_get_rates(call: ServiceCall) -> ServiceResponse:
            return await _get_rates(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_RATES,
            handle_get_rates,
            schema=_RATES_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_GET_EMHASS_FORECAST):

        async def handle_get_emhass_forecast(call: ServiceCall) -> ServiceResponse:
            return await _get_emhass_forecast(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_EMHASS_FORECAST,
            handle_get_emhass_forecast,
            schema=_EMHASS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_USAGE):

        async def handle_backfill_usage(call: ServiceCall) -> ServiceResponse:
            return await _backfill_usage(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_USAGE,
            handle_backfill_usage,
            schema=_BACKFILL_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )


__all__ = ["async_setup_services"]
