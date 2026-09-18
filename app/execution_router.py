from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.resilience import raise_for_status, with_retry
from app.schemas import ConfluenceReport, ExecutionPayload, Signal

logger = logging.getLogger(__name__)


class ExecutionRouter:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._s = settings
        self._http = http

    @staticmethod
    def _idempotency_key(report: ConfluenceReport) -> str:
        assert report.levels is not None
        hour_bucket = int(time.time() // 3600)
        raw = f"{report.symbol}|{report.levels.side.value}|{report.primary_timeframe.value}|{report.levels.entry:.6g}|{hour_bucket}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def build_payload(self, report: ConfluenceReport) -> ExecutionPayload:
        lv = report.levels
        assert lv is not None
        return ExecutionPayload(
            symbol=report.symbol,
            asset_class=report.asset_class,
            side=lv.side,
            timeframe=report.primary_timeframe,
            entry=lv.entry,
            stop_loss=lv.stop_loss,
            take_profits=[lv.tp1, lv.tp2, lv.tp3],
            score=report.score,
            rrr=lv.effective_rrr,
            position_units=lv.position_units,
            management=report.management,
            idempotency_key=self._idempotency_key(report),
            generated_at=datetime.now(UTC),
            dry_run=self._s.dry_run,
        )

    @staticmethod
    def sign(key: str, timestamp: str, body: str) -> str:
        return hmac.new(key.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()

    @with_retry(attempts=3, initial=1.0)
    async def _post(self, url: str, body: str, headers: dict[str, str]) -> None:
        resp = await self._http.post(url, content=body, headers=headers)
        raise_for_status(resp)

    async def dispatch(self, report: ConfluenceReport) -> str:
        if report.signal not in (Signal.BUY, Signal.SELL) or report.levels is None:
            return "skipped"
        if not self._s.execution_enabled:
            logger.info("execution disabled; signal not forwarded", extra={"symbol": report.symbol, "signal": report.signal.value})
            return "disabled"

        payload = self.build_payload(report)
        body = payload.model_dump_json()
        ts = str(int(time.time()))
        key = self._s.execution_hmac_key.get_secret_value() if self._s.execution_hmac_key else ""
        if not key:
            logger.error("EXECUTION_HMAC_KEY missing; refusing to send unsigned execution payload", extra={"symbol": report.symbol})
            return "failed"
        headers = {
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": self.sign(key, ts, body),
            "X-Idempotency-Key": payload.idempotency_key,
        }

        if not self._s.execution_live:
            logger.info("execution dry-run", extra={"payload": payload.model_dump(mode="json"), "dry_run": True})
            return "dry_run"

        assert self._s.execution_webhook_url is not None
        try:
            await self._post(self._s.execution_webhook_url, body, headers)
        except Exception as exc:  # noqa: BLE001
            logger.error("execution webhook failed", extra={"symbol": report.symbol, "error": str(exc)})
            return "failed"
        logger.info("execution webhook sent", extra={"symbol": report.symbol, "side": payload.side.value, "idempotency_key": payload.idempotency_key})
        return "sent"
