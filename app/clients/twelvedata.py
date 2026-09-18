from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.resilience import RateLimitedError, UpstreamError, raise_for_status, with_retry
from app.schemas import OHLCV, Candle, Timeframe
from app.timeframes import TWELVEDATA_INTERVAL as _INTERVAL


class TwelveDataClient:
    """Optional keyed stock/ETF candle source (free tier: 800 credits/day)."""

    def __init__(self, http: httpx.AsyncClient, api_key: str, base_url: str = "https://api.twelvedata.com") -> None:
        self._http = http
        self._key = api_key
        self._base = base_url.rstrip("/")

    @with_retry(attempts=3, initial=1.0)
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        resp = await self._http.get(
            f"{self._base}/time_series",
            params={
                "symbol": symbol, "interval": _INTERVAL[timeframe], "outputsize": min(limit, 5000),
                "timezone": "UTC", "format": "JSON", "apikey": self._key,
            },
        )
        raise_for_status(resp)
        data = resp.json()
        if data.get("status") == "error":
            message = str(data.get("message", "unknown error"))
            if data.get("code") == 429:
                raise RateLimitedError(f"twelvedata: {message}")
            raise UpstreamError(f"twelvedata: {message}")

        candles: list[Candle] = []
        for row in data.get("values") or []:
            try:
                dt = datetime.fromisoformat(str(row["datetime"]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                candles.append(
                    Candle(
                        ts=int(dt.timestamp() * 1000), open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]), volume=float(row.get("volume") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not candles:
            raise UpstreamError(f"twelvedata returned no candles for {symbol}")
        candles.sort(key=lambda c: c.ts)
        return OHLCV(symbol=symbol, timeframe=timeframe, candles=candles[-limit:], source="twelvedata")
