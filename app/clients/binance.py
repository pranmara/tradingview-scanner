from __future__ import annotations

import httpx

from app.resilience import UpstreamError, raise_for_status, with_retry
from app.schemas import OHLCV, Candle, Timeframe
from app.timeframes import BINANCE_INTERVAL


class BinanceClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str = "https://api.binance.com") -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    @with_retry()
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        resp = await self._http.get(
            f"{self._base}/api/v3/klines",
            params={"symbol": symbol, "interval": BINANCE_INTERVAL[timeframe], "limit": min(limit, 1000)},
        )
        raise_for_status(resp)
        rows = resp.json()
        candles = [
            Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in rows
        ]
        if not candles:
            raise UpstreamError(f"binance returned no candles for {symbol}")
        return OHLCV(symbol=symbol, timeframe=timeframe, candles=candles, source="binance")

    async def get_ohlcv_history(self, symbol: str, timeframe: Timeframe, bars: int) -> OHLCV:
        """Page backwards through /klines (1000 per call) to collect `bars` candles."""
        collected: list[Candle] = []
        end_time: int | None = None
        while len(collected) < bars:
            params: dict[str, str | int] = {"symbol": symbol, "interval": BINANCE_INTERVAL[timeframe], "limit": min(1000, bars - len(collected))}
            if end_time is not None:
                params["endTime"] = end_time
            resp = await self._http.get(f"{self._base}/api/v3/klines", params=params)
            raise_for_status(resp)
            rows = resp.json()
            if not rows:
                break
            page = [Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=float(r[5])) for r in rows]
            collected = page + collected
            end_time = page[0].ts - 1
            if len(rows) < params["limit"]:
                break
        if not collected:
            raise UpstreamError(f"binance returned no history for {symbol}")
        return OHLCV(symbol=symbol, timeframe=timeframe, candles=collected[-bars:], source="binance")

    async def ping(self) -> bool:
        try:
            resp = await self._http.get(f"{self._base}/api/v3/ping", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
