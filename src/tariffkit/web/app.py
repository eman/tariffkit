"""A small read-only REST API over the rate engine.

Every response is pure computation over vendored data, so there is nothing to
cache invalidate and no upstream to rate-limit.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..engine import RateEngine
from ..errors import ConfigError, DataError, OutOfRangeError

if TYPE_CHECKING:
    from fastapi import FastAPI

MAX_FORECAST_HOURS = 24 * 365


def create_app(config: Config | None = None) -> FastAPI:
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
    except ImportError as exc:  # pragma: no cover - exercised by packaging
        raise RuntimeError(
            "web support requires the 'web' extra: pip install 'tariffkit[web]'"
        ) from exc

    engine = RateEngine(config or Config.load())

    def request_engine(raw: object) -> RateEngine:
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
        return engine.describe()

    @app.post("/v1/meta")
    def configured_meta(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        return request_engine(payload.get("config")).describe()

    @app.get("/v1/price/now")
    def price_now() -> dict[str, Any]:
        return engine.price_now().to_dict()

    @app.post("/v1/price/now")
    def configured_price_now(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        return request_engine(payload.get("config")).price_now().to_dict()

    @app.get("/v1/price/at")
    def price_at(
        ts: datetime = Query(description="ISO 8601 timestamp with offset"),
    ) -> dict[str, Any]:
        if ts.tzinfo is None:
            raise HTTPException(422, "ts must include a UTC offset, e.g. 2026-09-15T19:00-07:00")
        try:
            return engine.price_at(ts).to_dict()
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/price/at")
    def configured_price_at(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        request = request_engine(payload.get("config"))
        ts = request_timestamp(payload.get("ts"), "ts")
        try:
            return request.price_at(ts).to_dict()
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
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/forecast")
    def configured_forecast(payload: dict[str, Any] = Body()) -> dict[str, Any]:
        request = request_engine(payload.get("config"))
        hours = payload.get("hours", 24)
        if not isinstance(hours, int) or isinstance(hours, bool):
            raise HTTPException(422, "hours must be an integer")
        if not 1 <= hours <= MAX_FORECAST_HOURS:
            raise HTTPException(422, f"hours must be between 1 and {MAX_FORECAST_HOURS}")
        raw_start = payload.get("start")
        start = request_timestamp(raw_start, "start") if raw_start is not None else None
        try:
            return request.forecast(hours=hours, start=start).to_dict()
        except OutOfRangeError as exc:
            raise HTTPException(404, str(exc)) from exc
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


def _version() -> str:
    from .. import __version__

    return __version__
