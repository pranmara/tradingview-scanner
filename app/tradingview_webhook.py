from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import ValidationError

from app.alert_store import AlertStore
from app.asset_classifier import classify
from app.config import Settings
from app.schemas import PineAlert, PineAlertIn, Timeframe
from app.timeframes import normalize_timeframe_label

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_BODY_BYTES = 8_192


def _client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> AlertStore:
    return request.app.state.alert_store


@router.post("/webhooks/tradingview", status_code=status.HTTP_202_ACCEPTED)
async def tradingview_webhook(request: Request, x_signature: str | None = Header(default=None)) -> dict[str, str]:
    settings = _settings(request)
    ip = _client_ip(request, settings.tv_webhook_trust_proxy)
    if settings.tv_webhook_enforce_ip_allowlist and ip not in settings.webhook_ip_allowlist:
        logger.warning("webhook rejected: ip not allowlisted", extra={"ip": ip})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "source not allowed")

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")

    if settings.tv_webhook_hmac_key is not None:
        expected = hmac.new(settings.tv_webhook_hmac_key.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
        if not x_signature or not hmac.compare_digest(x_signature.strip().lower(), expected):
            logger.warning("webhook rejected: bad hmac signature", extra={"ip": ip})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        payload = PineAlertIn.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid payload: {exc.error_count()} error(s)") from exc

    if not hmac.compare_digest(payload.secret_key.encode(), settings.tv_webhook_secret.get_secret_value().encode()):
        logger.warning("webhook rejected: bad secret", extra={"ip": ip, "ticker": payload.ticker})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid secret")

    now_ms = int(time.time() * 1000)
    alert_ts = payload.alert_epoch_ms()
    if alert_ts is not None and abs(now_ms - alert_ts) > settings.tv_webhook_max_skew_seconds * 1000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "alert timestamp outside allowed skew")

    try:
        asset = classify(payload.ticker)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    timeframe = normalize_timeframe_label(payload.timeframe)

    values_sig = hashlib.sha1(repr(sorted(payload.values.items())).encode()).hexdigest()[:12] if payload.values else ""
    dedupe_raw = f"{asset.symbol}|{timeframe}|{payload.indicator}|{payload.signal.value}|{alert_ts or payload.price}|{values_sig}"
    dedupe_key = hashlib.sha256(dedupe_raw.encode()).hexdigest()
    if not await _store(request).claim(dedupe_key):
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate alert")

    alert = PineAlert(
        ticker=asset.symbol,
        timeframe=timeframe,
        indicator=payload.indicator,
        signal=payload.signal,
        price=payload.price,
        values=payload.values,
        alert_ts_ms=alert_ts,
        received_at_ms=now_ms,
    )
    await _store(request).add(alert)
    logger.info("pine alert accepted", extra={"ticker": alert.ticker, "timeframe": alert.timeframe, "indicator": alert.indicator,
                                              "signal": alert.signal.value, "price": alert.price, "values": alert.values})
    return {"status": "accepted", "ticker": alert.ticker, "timeframe": alert.timeframe}


@router.get("/indicators/{ticker}")
async def latest_indicator_values(ticker: str, request: Request, x_webhook_secret: str | None = Header(default=None)) -> dict[str, dict]:
    """Latest values per (indicator, timeframe) — handy for checking a Pine bridge is streaming."""
    settings = _settings(request)
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret.encode(), settings.tv_webhook_secret.get_secret_value().encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid secret")
    try:
        asset = classify(ticker)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    latest: dict[str, dict] = {}
    for a in await _store(request).recent(asset.symbol, [tf.value for tf in Timeframe]):
        latest.setdefault(f"{a.indicator}@{a.timeframe}", {"signal": a.signal.value, "price": a.price, "values": a.values,
                                                          "received_at_ms": a.received_at_ms})
    return latest


@router.get("/alerts/{ticker}")
async def list_alerts(ticker: str, request: Request, x_webhook_secret: str | None = Header(default=None)) -> list[dict]:
    settings = _settings(request)
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret.encode(), settings.tv_webhook_secret.get_secret_value().encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid secret")
    try:
        asset = classify(ticker)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    alerts = await _store(request).recent(asset.symbol, [tf.value for tf in Timeframe])
    return [a.model_dump() for a in alerts]


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    if not await _store(request).ping():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "redis unavailable")
    return {"status": "ready"}
