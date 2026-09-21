from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.resilience import UpstreamError, raise_for_status, with_retry
from app.schemas import Candle

# USDT-margined perpetuals. Funding history is free and deep — back to Sep 2019 for BTCUSDT — which makes it
# the one positioning dataset that can be backtested without paying for data. Open interest and long/short
# ratio are NOT here: Binance serves only 30 days of those, so they cannot be backtested from this venue.

_FUNDING_PAGE = 1000
_KLINE_PAGE = 1500


@dataclass(frozen=True)
class FundingPoint:
    ts: int      # funding time, epoch ms
    rate: float  # per 8h period; positive means longs pay shorts


class BinanceFuturesClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str = "https://fapi.binance.com") -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    @with_retry()
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.get(f"{self._base}{path}", params=params or {})
        raise_for_status(resp)
        return resp.json()

    async def funding_history(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[FundingPoint]:
        """Pages FORWARD — this endpoint returns ascending — until it runs out or passes end_ms."""
        out: list[FundingPoint] = []
        cursor = start_ms
        while True:
            params: dict[str, Any] = {"symbol": symbol, "startTime": cursor, "limit": _FUNDING_PAGE}
            if end_ms is not None:
                params["endTime"] = end_ms
            rows = await self._get("/fapi/v1/fundingRate", params)
            if not rows:
                break
            page = [FundingPoint(ts=int(r["fundingTime"]), rate=float(r["fundingRate"])) for r in rows]
            out.extend(p for p in page if not out or p.ts > out[-1].ts)
            if len(rows) < _FUNDING_PAGE:
                break
            cursor = page[-1].ts + 1
        return out

    async def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int | None = None) -> list[Candle]:
        """Perp klines, not spot: a funding strategy holds the perp, so its price is the one that matters."""
        out: list[Candle] = []
        cursor = start_ms
        while True:
            params: dict[str, Any] = {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": _KLINE_PAGE}
            if end_ms is not None:
                params["endTime"] = end_ms
            rows = await self._get("/fapi/v1/klines", params)
            if not rows:
                break
            page = [Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                           close=float(r[4]), volume=float(r[5])) for r in rows]
            out.extend(c for c in page if not out or c.ts > out[-1].ts)
            if len(rows) < _KLINE_PAGE:
                break
            cursor = page[-1].ts + 1
        if not out:
            raise UpstreamError(f"binance futures returned no klines for {symbol}")
        return out

    async def usdt_perpetuals_by_volume(self, limit: int) -> list[str]:
        """Currently-trading USDT perpetuals, largest 24h quote volume first."""
        info, tickers = await self._get("/fapi/v1/exchangeInfo"), await self._get("/fapi/v1/ticker/24hr")
        live = {s["symbol"] for s in info.get("symbols", [])
                if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
        ranked = sorted((t for t in tickers if t.get("symbol") in live), key=lambda t: -float(t.get("quoteVolume", 0)))
        return [t["symbol"] for t in ranked[:limit]]


__all__ = ["BinanceFuturesClient", "FundingPoint"]
