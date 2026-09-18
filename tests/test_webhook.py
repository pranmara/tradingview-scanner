from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.alert_store import AlertStore
from app.config import Settings
from app.tradingview_webhook import router


def _client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    app.state.alert_store = AlertStore(None, 3600)
    return TestClient(app)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ticker": "BINANCE:BTCUSDT", "timeframe": "240", "indicator": "SuperTrend_V2",
        "signal": "BUY", "price": 65000.5, "timestamp": int(time.time() * 1000), "secret_key": "test-secret",
    }
    base.update(overrides)
    return base


def test_accepts_valid_alert(settings: Settings) -> None:
    client = _client(settings)
    resp = client.post("/webhooks/tradingview", content=json.dumps(_payload()), headers={"Content-Type": "text/plain"})
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"status": "accepted", "ticker": "BTCUSDT", "timeframe": "4h"}
    listed = client.get("/alerts/btcusdt", headers={"X-Webhook-Secret": "test-secret"})
    assert listed.status_code == 200 and listed.json()[0]["indicator"] == "SuperTrend_V2"


def test_state_alert_with_values(settings: Settings) -> None:
    client = _client(settings)
    body = _payload(indicator="Bridge", signal="NEUTRAL", values={"src1": 1.25, "src2": "3.5", "bad": "x", "flag": True})
    assert client.post("/webhooks/tradingview", json=body).status_code == 202
    body2 = _payload(indicator="Bridge", signal="NEUTRAL", values={"src1": 1.30})
    assert client.post("/webhooks/tradingview", json=body2).status_code == 202  # different values -> not a duplicate
    latest = client.get("/indicators/BTCUSDT", headers={"X-Webhook-Secret": "test-secret"}).json()
    assert latest["Bridge@4h"]["values"] == {"src1": 1.30}
    assert latest["Bridge@4h"]["signal"] == "NEUTRAL"


def test_rejects_bad_secret(settings: Settings) -> None:
    resp = _client(settings).post("/webhooks/tradingview", json=_payload(secret_key="nope"))
    assert resp.status_code == 401


def test_rejects_duplicate(settings: Settings) -> None:
    client = _client(settings)
    body = _payload()
    assert client.post("/webhooks/tradingview", json=body).status_code == 202
    assert client.post("/webhooks/tradingview", json=body).status_code == 409


def test_rejects_stale_timestamp(settings: Settings) -> None:
    resp = _client(settings).post("/webhooks/tradingview", json=_payload(timestamp=int(time.time() * 1000) - 3_600_000))
    assert resp.status_code == 400


def test_ip_allowlist_enforced() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="test-secret",  # type: ignore[call-arg]
                        tv_webhook_enforce_ip_allowlist=True, redis_url=None)
    resp = _client(settings).post("/webhooks/tradingview", json=_payload())
    assert resp.status_code == 403


def test_hmac_header_when_configured() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="test-secret",  # type: ignore[call-arg]
                        tv_webhook_hmac_key="relay-key", tv_webhook_enforce_ip_allowlist=False, redis_url=None)
    client = _client(settings)
    raw = json.dumps(_payload()).encode()
    assert client.post("/webhooks/tradingview", content=raw).status_code == 401
    sig = hmac.new(b"relay-key", raw, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/tradingview", content=raw, headers={"X-Signature": sig}).status_code == 202


def test_health(settings: Settings) -> None:
    client = _client(settings)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.head("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
