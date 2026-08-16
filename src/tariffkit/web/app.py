"""A small read-only REST API over the rate engine.

Every response is pure computation over vendored data, so there is nothing to
cache invalidate and no upstream to rate-limit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..account import (
    AccountError,
    AccountRateEngine,
    NamedProfileRepository,
    ProfileNotFoundError,
    configured_profile_name,
)
from ..config import Config
from ..engine import RateEngine
from ..errors import ConfigError, DataError, OutOfRangeError

if TYPE_CHECKING:
    from fastapi import FastAPI

MAX_FORECAST_HOURS = 24 * 365
_PROFILE_UNAVAILABLE = "profile unavailable"


def create_app(
    config: Config | None = None,
    *,
    profile_name: str | None = None,
    profile_repository: NamedProfileRepository | None = None,
    config_path: str | Path | None = None,
) -> FastAPI:
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
    except ImportError as exc:  # pragma: no cover - exercised by packaging
        raise RuntimeError(
            "web support requires the 'web' extra: pip install 'tariffkit[web]'"
        ) from exc

    def load_profile(name: str) -> AccountRateEngine:
        repository = profile_repository or NamedProfileRepository()
        try:
            return AccountRateEngine(repository.load(name))
        except ProfileNotFoundError as exc:
            # Do not reveal whether a profile exists, nor details from its
            # managed file, through a request-facing error.
            raise HTTPException(404, _PROFILE_UNAVAILABLE) from exc
        except AccountError as exc:
            raise HTTPException(404, _PROFILE_UNAVAILABLE) from exc

    selected_profile = profile_name
    if selected_profile is None and config is None:
        selected_profile = configured_profile_name(config_path)
    engine: RateEngine | AccountRateEngine
    if selected_profile is not None:
        engine = load_profile(selected_profile)
    else:
        engine = RateEngine(config or Config.load(config_path))

    def request_engine(
        payload: dict[str, Any], *, allowed: set[str]
    ) -> RateEngine | AccountRateEngine:
        unknown = set(payload) - allowed
        if unknown:
            raise HTTPException(422, f"unknown request keys: {sorted(unknown)}")
        raw = payload.get("config")
        requested_profile = payload.get("profile")
        requested_account = payload.get("account")
        if requested_profile is not None and requested_account is not None:
            if requested_profile != requested_account:
                raise HTTPException(422, "profile and account selections disagree")
        elif requested_profile is None:
            requested_profile = requested_account
        if raw is not None and requested_profile is not None:
            raise HTTPException(422, "choose either config or profile")
        if requested_profile is not None:
            if not isinstance(requested_profile, str):
                raise HTTPException(404, _PROFILE_UNAVAILABLE)
            return load_profile(requested_profile)
        if raw is None:
            if selected_profile is not None:
                return engine
            raise HTTPException(422, "config must be a JSON object or profile must be selected")
        if not isinstance(raw, dict):
            raise HTTPException(422, "config must be a JSON object")
        try:
            return RateEngine(Config.from_dict(raw))
        except (ConfigError, DataError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def request_timestamp(raw: object, name: str) -> datetime:
        if not isinstance(raw, str):
            raise HTTPException(422, f"{name} must be an ISO 8601 string")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(422, f"{name} must be an ISO 8601 string") from exc
        if parsed.tzinfo is None:
            raise HTTPException(422, f"{name} must include a UTC offset")
        return parsed

    app = FastAPI(
        title="tariffkit",
        summary="PG&E E-ELEC import/export prices under NEM 3.0",
        version=_version(),
    )

    @app.get("/v1/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/meta")
    def meta() -> dict[str, Any]:
        try:
            return engine.describe()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/meta")
    def configured_meta(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        try:
            return request_engine(payload, allowed={"config", "profile", "account"}).describe()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/price/now")
    def price_now() -> dict[str, Any]:
        try:
            return engine.price_now().to_dict()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/price/now")
    def configured_price_now(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        try:
            return (
                request_engine(payload, allowed={"config", "profile", "account"})
                .price_now()
                .to_dict()
            )
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/price/at")
    def price_at(
        ts: datetime = Query(description="ISO 8601 timestamp with offset"),
    ) -> dict[str, Any]:
        if ts.tzinfo is None:
            raise HTTPException(422, "ts must include a UTC offset, e.g. 2026-09-15T19:00-07:00")
        try:
            return engine.price_at(ts).to_dict()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/price/at")
    def configured_price_at(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        request = request_engine(payload, allowed={"config", "profile", "account", "ts"})
        ts = request_timestamp(payload.get("ts"), "ts")
        try:
            return request.price_at(ts).to_dict()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/forecast")
    def forecast(
        hours: int = Query(24, ge=1, le=MAX_FORECAST_HOURS),
        start: datetime | None = Query(None, description="defaults to the current hour"),
    ) -> dict[str, Any]:
        if start is not None and start.tzinfo is None:
            raise HTTPException(422, "start must include a UTC offset")
        try:
            return engine.forecast(hours=hours, start=start).to_dict()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/forecast")
    def configured_forecast(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        request = request_engine(
            payload, allowed={"config", "profile", "account", "hours", "start"}
        )
        hours = payload.get("hours", 24)
        if not isinstance(hours, int) or isinstance(hours, bool):
            raise HTTPException(422, "hours must be an integer")
        if not 1 <= hours <= MAX_FORECAST_HOURS:
            raise HTTPException(422, f"hours must be between 1 and {MAX_FORECAST_HOURS}")
        raw_start = payload.get("start")
        start = request_timestamp(raw_start, "start") if raw_start is not None else None
        try:
            return request.forecast(hours=hours, start=start).to_dict()
        except AccountError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


def _version() -> str:
    from .. import __version__

    return __version__
