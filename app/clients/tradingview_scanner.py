from __future__ import annotations

import httpx

from app.resilience import raise_for_status, with_retry
from app.schemas import TechnicalSnapshot, Timeframe
from app.timeframes import TV_SCANNER_SUFFIX

_COLUMNS = (
    "close", "EMA20", "EMA50", "EMA200", "RSI", "BB.upper", "BB.lower", "ATR", "volume",
    "Recommend.All", "Recommend.MA", "Recommend.Other",
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}


def _f(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


class TradingViewScannerClient:
    def __init__(self, http: httpx.AsyncClient, stock_market: str = "america") -> None:
        self._http = http
        self._stock_market = stock_market

    @with_retry(attempts=3)
    async def get_snapshot(self, tickers: list[str], timeframe: Timeframe, is_crypto: bool) -> TechnicalSnapshot | None:
        market = "crypto" if is_crypto else self._stock_market
        suffix = TV_SCANNER_SUFFIX[timeframe]
        columns = [f"{c}{suffix}" for c in _COLUMNS]
        if not is_crypto:
            columns.append("sector")
        body = {"symbols": {"tickers": tickers, "query": {"types": []}}, "columns": columns}
        resp = await self._http.post(f"https://scanner.tradingview.com/{market}/scan", json=body, headers=_HEADERS)
        raise_for_status(resp)
        rows = resp.json().get("data") or []
        if not rows:
            return None
        # TradingView only returns the tickers it recognises, and not necessarily in the order asked. Pick the
        # earliest candidate we requested so BINANCE beats BYBIT (and NASDAQ beats NYSE) when a symbol is on both.
        order = {t.upper(): i for i, t in enumerate(tickers)}
        row = min(rows, key=lambda r: order.get(str(r.get("s", "")).upper(), len(order)))
        values = dict(zip(columns, row.get("d") or [], strict=False))

        def g(name: str) -> float | None:
            return _f(values.get(f"{name}{suffix}"))

        return TechnicalSnapshot(
            symbol=str(row.get("s", tickers[0])),
            timeframe=timeframe,
            source="tv-scanner",
            close=g("close"),
            ema20=g("EMA20"),
            ema50=g("EMA50"),
            ema200=g("EMA200"),
            rsi=g("RSI"),
            bb_upper=g("BB.upper"),
            bb_lower=g("BB.lower"),
            atr=g("ATR"),
            volume=g("volume"),
            recommend_all=g("Recommend.All"),
            recommend_ma=g("Recommend.MA"),
            recommend_osc=g("Recommend.Other"),
            sector=values.get("sector") if isinstance(values.get("sector"), str) else None,
        )
