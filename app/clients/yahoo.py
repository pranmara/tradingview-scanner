from __future__ import annotations

import asyncio
import logging
from itertools import groupby

import httpx

from app.resilience import UpstreamError, raise_for_status, with_retry
from app.schemas import OHLCV, Candle, Timeframe
from app.timeframes import YAHOO_FETCH

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
_DAY_MS = 86_400_000


def resample(candles: list[Candle], factor: int) -> list[Candle]:
    """Aggregate intraday bars in groups of `factor`, never crossing a UTC day boundary."""
    out: list[Candle] = []
    for _, day_iter in groupby(candles, key=lambda c: c.ts // _DAY_MS):
        day = list(day_iter)
        for i in range(0, len(day), factor):
            chunk = day[i : i + factor]
            out.append(
                Candle(
                    ts=chunk[0].ts,
                    open=chunk[0].open,
                    high=max(c.high for c in chunk),
                    low=min(c.low for c in chunk),
                    close=chunk[-1].close,
                    volume=sum(c.volume for c in chunk),
                )
            )
    return out


class YahooClient:
    """Yahoo chart API. Requests are serialised and carry the cookie+crumb Yahoo now expects."""

    def __init__(self, http: httpx.AsyncClient, base_url: str = "https://query1.finance.yahoo.com/v8/finance/chart") -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._crumb: str | None = None
        self._crumb_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(1)

    async def _ensure_crumb(self) -> str | None:
        if self._crumb:
            return self._crumb
        async with self._crumb_lock:
            if self._crumb:
                return self._crumb
            try:
                await self._http.get("https://fc.yahoo.com", headers=_HEADERS)  # 404 is expected; sets the A3 cookie
                resp = await self._http.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=_HEADERS)
                text = resp.text.strip()
                if resp.status_code == 200 and text and "<" not in text:
                    self._crumb = text
            except httpx.HTTPError as exc:
                logger.debug("yahoo crumb bootstrap failed", extra={"error": str(exc)})
            return self._crumb

    @with_retry(attempts=3, initial=1.5, maximum=6.0)
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        interval, range_, factor = YAHOO_FETCH[timeframe]
        async with self._sem:
            crumb = await self._ensure_crumb()
            params = {"interval": interval, "range": range_, "includePrePost": "false", "events": ""}
            if crumb:
                params["crumb"] = crumb
            resp = await self._http.get(f"{self._base}/{symbol}", params=params, headers=_HEADERS)
            if resp.status_code in (401, 403, 429):
                self._crumb = None
            raise_for_status(resp)

        chart = resp.json().get("chart") or {}
        result = chart.get("result") or []
        if not result:
            err = (chart.get("error") or {}).get("description", "unknown error")
            raise UpstreamError(f"yahoo: {symbol}: {err}")

        block = result[0]
        stamps = block.get("timestamp") or []
        quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes, vols = quote.get("low") or [], quote.get("close") or [], quote.get("volume") or []

        candles: list[Candle] = []
        for i, t in enumerate(stamps):
            try:
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            except IndexError:
                break
            if None in (o, h, l, c):
                continue
            v = vols[i] if i < len(vols) and vols[i] is not None else 0.0
            candles.append(Candle(ts=int(t) * 1000, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v)))

        if factor > 1:
            candles = resample(candles, factor)
        if not candles:
            raise UpstreamError(f"yahoo returned no candles for {symbol}")
        return OHLCV(symbol=symbol, timeframe=timeframe, candles=candles[-limit:], source="yahoo")
