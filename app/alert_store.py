from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque

from redis.asyncio import Redis

from app.schemas import PineAlert

logger = logging.getLogger(__name__)


class AlertStore:
    """Recent Pine Script alerts keyed by ticker + timeframe. Redis-backed with in-memory fallback."""

    def __init__(self, redis: Redis | None, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._mem: dict[str, deque[PineAlert]] = defaultdict(lambda: deque(maxlen=200))
        self._dedupe: dict[str, float] = {}

    @staticmethod
    def _key(ticker: str, timeframe: str) -> str:
        return f"alerts:{ticker.upper()}:{timeframe}"

    async def add(self, alert: PineAlert) -> None:
        key = self._key(alert.ticker, alert.timeframe)
        if self._redis is None:
            self._mem[key].append(alert)
            return
        cutoff = int(time.time() * 1000) - self._ttl * 1000
        pipe = self._redis.pipeline()
        pipe.zadd(key, {alert.model_dump_json(): alert.received_at_ms})
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def recent(self, ticker: str, timeframes: list[str]) -> list[PineAlert]:
        cutoff = int(time.time() * 1000) - self._ttl * 1000
        out: list[PineAlert] = []
        for tf in timeframes:
            key = self._key(ticker, tf)
            if self._redis is None:
                out.extend(a for a in self._mem[key] if a.received_at_ms >= cutoff)
                continue
            raw = await self._redis.zrangebyscore(key, cutoff, "+inf")
            for item in raw:
                try:
                    out.append(PineAlert.model_validate_json(item))
                except ValueError:
                    logger.warning("dropping malformed stored alert", extra={"key": key})
        out.sort(key=lambda a: a.received_at_ms, reverse=True)
        return out

    async def claim(self, dedupe_key: str, ttl_seconds: int = 600) -> bool:
        """True if this key was not seen before (and is now claimed)."""
        if self._redis is None:
            now = time.time()
            expired = [k for k, exp in self._dedupe.items() if exp < now]
            for k in expired:
                del self._dedupe[k]
            if dedupe_key in self._dedupe:
                return False
            self._dedupe[dedupe_key] = now + ttl_seconds
            return True
        return bool(await self._redis.set(f"dedupe:{dedupe_key}", "1", nx=True, ex=ttl_seconds))

    async def ping(self) -> bool:
        if self._redis is None:
            return True
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def to_json(alerts: list[PineAlert]) -> str:
        return json.dumps([a.model_dump() for a in alerts])
