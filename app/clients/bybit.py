from __future__ import annotations

import logging
from typing import Any

import httpx

from app.resilience import RetryableError, UpstreamError, raise_for_status, with_retry
from app.schemas import OHLCV, Candle, Timeframe
from app.timeframes import BYBIT_INTERVAL

logger = logging.getLogger(__name__)

# Bybit v5 answers 200 OK with a non-zero retCode for business errors, so HTTP status alone is not enough.
# Spot is tried first; plenty of newer tokens list as a perpetual before they ever get a spot pair, and the
# linear contract carries the same BASE+QUOTE ticker, so the same symbol works for both.
DEFAULT_CATEGORIES: tuple[str, ...] = ("spot", "linear")
_MAX_LIMIT = 1000
_RETRYABLE_CODES = frozenset({10002, 10006, 10016, 10018})  # timestamp skew, rate limit, service busy


class BybitClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str = "https://api.bybit.com",
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._categories = categories

    async def _klines(self, symbol: str, timeframe: Timeframe, limit: int, category: str,
                      end_ms: int | None = None) -> list[Candle]:
        params: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "interval": BYBIT_INTERVAL[timeframe],
            "limit": min(limit, _MAX_LIMIT),
        }
        if end_ms is not None:
            params["end"] = end_ms
        resp = await self._http.get(f"{self._base}/v5/market/kline", params=params)
        raise_for_status(resp)
        body = resp.json()
        code = int(body.get("retCode", -1))
        if code != 0:
            message = str(body.get("retMsg", "unknown error"))[:160]
            if code in _RETRYABLE_CODES:
                raise RetryableError(f"bybit {category} retCode {code}: {message}")
            raise UpstreamError(f"bybit {category} retCode {code}: {message}")
        rows = (body.get("result") or {}).get("list") or []
        # v5 returns newest first: [startTime, open, high, low, close, volume, turnover]
        return [
            Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                   volume=float(r[5]))
            for r in reversed(rows)
        ]

    @with_retry()
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        errors: list[str] = []
        for category in self._categories:
            try:
                candles = await self._klines(symbol, timeframe, limit, category)
            except UpstreamError as exc:
                errors.append(str(exc))
                continue
            if candles:
                return OHLCV(symbol=symbol, timeframe=timeframe, candles=candles, source=f"bybit-{category}")
            errors.append(f"bybit {category}: no candles")
        raise UpstreamError(f"bybit returned no candles for {symbol}: " + " | ".join(errors))

    @with_retry()
    async def get_ohlcv_history(self, symbol: str, timeframe: Timeframe, bars: int) -> OHLCV:
        """Page backwards through /v5/market/kline to collect `bars` candles, mirroring the Binance client."""
        for category in self._categories:
            collected: list[Candle] = []
            end_ms: int | None = None
            while len(collected) < bars:
                want = min(_MAX_LIMIT, bars - len(collected))
                try:
                    page = await self._klines(symbol, timeframe, want, category, end_ms=end_ms)
                except UpstreamError:
                    break
                if not page:
                    break
                collected = page + collected
                end_ms = page[0].ts - 1
                if len(page) < want:
                    break
            if collected:
                return OHLCV(symbol=symbol, timeframe=timeframe, candles=collected[-bars:], source=f"bybit-{category}")
        raise UpstreamError(f"bybit returned no history for {symbol}")

    async def ping(self) -> bool:
        try:
            resp = await self._http.get(f"{self._base}/v5/market/time", timeout=5.0)
            return resp.status_code == 200 and int(resp.json().get("retCode", -1)) == 0
        except (httpx.HTTPError, ValueError):
            return False


__all__ = ["BybitClient", "DEFAULT_CATEGORIES"]
