from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from mcp import ClientSession

try:  # mcp >= 2.0 yields (read, write); mcp 1.8-1.x yields (read, write, get_session_id)
    from mcp.client.streamable_http import streamable_http_client as _streamable_http_client
except ImportError:  # pragma: no cover - depends on installed mcp major version
    from mcp.client.streamable_http import streamablehttp_client as _streamable_http_client  # type: ignore[no-redef]

from app.resilience import UpstreamError, with_retry
from app.schemas import OHLCV, Candle, TechnicalSnapshot, Timeframe

logger = logging.getLogger(__name__)

_TS_KEYS = ("ts", "time", "timestamp", "t", "datetime", "date", "open_time", "openTime")
_OPEN_KEYS, _HIGH_KEYS = ("open", "o", "Open"), ("high", "h", "High")
_LOW_KEYS, _CLOSE_KEYS = ("low", "l", "Low"), ("close", "c", "Close")
_VOL_KEYS = ("volume", "v", "vol", "Volume")
_ROW_CONTAINERS = ("candles", "data", "bars", "ohlcv", "klines", "result", "items", "values")
_RATING_TEXT = {"STRONG_BUY": 0.75, "BUY": 0.3, "NEUTRAL": 0.0, "SELL": -0.3, "STRONG_SELL": -0.75}


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _ts_ms(v: Any) -> int | None:
    if isinstance(v, (int, float)):
        val = float(v)
        return int(val * 1000) if val < 1e11 else int(val)
    if isinstance(v, str):
        s = v.strip()
        if s.replace(".", "", 1).isdigit():
            return _ts_ms(float(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    return None


def _rows(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _ROW_CONTAINERS:
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                found = _rows(inner)
                if found:
                    return found
        if isinstance(data.get("close"), list):
            n = len(data["close"])
            stamps = _first(data, _TS_KEYS) or list(range(n))
            vols = data.get("volume") or [0.0] * n
            return [
                {"ts": stamps[i], "open": data["open"][i], "high": data["high"][i], "low": data["low"][i],
                 "close": data["close"][i], "volume": vols[i]}
                for i in range(n)
            ]
    return []


def normalize_candles(data: Any) -> list[Candle]:
    out: list[Candle] = []
    for row in _rows(data):
        try:
            if isinstance(row, dict):
                ts = _ts_ms(_first(row, _TS_KEYS))
                o, h = _first(row, _OPEN_KEYS), _first(row, _HIGH_KEYS)
                lo, c = _first(row, _LOW_KEYS), _first(row, _CLOSE_KEYS)
                v = _first(row, _VOL_KEYS) or 0.0
            elif isinstance(row, (list, tuple)) and len(row) >= 5:
                ts, o, h, lo, c = _ts_ms(row[0]), row[1], row[2], row[3], row[4]
                v = row[5] if len(row) > 5 else 0.0
            else:
                continue
            if ts is None or None in (o, h, lo, c):
                continue
            out.append(Candle(ts=ts, open=float(o), high=float(h), low=float(lo), close=float(c), volume=float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.ts)
    return out


def _flatten(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    flat: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    flat.update({k: v for k, v in data.items() if not isinstance(v, dict)})
    return flat


def _num(d: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    v = _first(d, keys)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        upper = v.strip().upper().replace(" ", "_")
        if upper in _RATING_TEXT:
            return _RATING_TEXT[upper]
        try:
            return float(v)
        except ValueError:
            return None
    return None


def normalize_snapshot(data: Any, symbol: str, timeframe: Timeframe) -> TechnicalSnapshot | None:
    d = _flatten(data)
    if not d:
        return None
    snap = TechnicalSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        source="tv-mcp",
        close=_num(d, ("close", "price", "last")),
        rsi=_num(d, ("RSI", "rsi", "rsi14", "RSI14")),
        ema20=_num(d, ("EMA20", "ema20", "ema_20")),
        ema50=_num(d, ("EMA50", "ema50", "ema_50")),
        ema200=_num(d, ("EMA200", "ema200", "ema_200")),
        bb_upper=_num(d, ("BB.upper", "bb_upper", "bbUpper")),
        bb_lower=_num(d, ("BB.lower", "bb_lower", "bbLower")),
        atr=_num(d, ("ATR", "atr", "atr14")),
        volume=_num(d, ("volume", "vol")),
        recommend_all=_num(d, ("Recommend.All", "recommendation", "recommend_all", "rating", "summary")),
        recommend_ma=_num(d, ("Recommend.MA", "recommend_ma", "moving_averages")),
        recommend_osc=_num(d, ("Recommend.Other", "recommend_osc", "oscillators")),
        sector=d.get("sector") if isinstance(d.get("sector"), str) else None,
    )
    return snap if any(v is not None for v in (snap.close, snap.rsi, snap.recommend_all, snap.ema20)) else None


class TradingViewMCPClient:
    def __init__(self, url: str, ohlcv_tool: str, snapshot_tool: str, timeout_seconds: float = 20.0) -> None:
        self._url = url
        self._ohlcv_tool = ohlcv_tool
        self._snapshot_tool = snapshot_tool
        self._timeout = timeout_seconds

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        async with asyncio.timeout(self._timeout):
            async with _streamable_http_client(self._url) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args)
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        joined = "\n".join(texts)
        if getattr(result, "isError", False):
            raise UpstreamError(f"mcp tool {tool} error: {joined[:300]}")
        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return joined

    @with_retry(attempts=3, initial=1.0)
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        data = await self._call(
            self._ohlcv_tool,
            {"symbol": symbol, "timeframe": timeframe.value, "interval": timeframe.value, "limit": limit, "bars": limit},
        )
        candles = normalize_candles(data)
        if not candles:
            raise UpstreamError(f"mcp tool {self._ohlcv_tool} returned no candles for {symbol}")
        return OHLCV(symbol=symbol, timeframe=timeframe, candles=candles[-limit:], source="tv-mcp")

    @with_retry(attempts=2, initial=1.0)
    async def get_snapshot(self, symbol: str, timeframe: Timeframe) -> TechnicalSnapshot | None:
        data = await self._call(
            self._snapshot_tool, {"symbol": symbol, "timeframe": timeframe.value, "interval": timeframe.value}
        )
        return normalize_snapshot(data, symbol, timeframe)

    async def list_tools(self) -> list[str]:
        async with asyncio.timeout(self._timeout):
            async with _streamable_http_client(self._url) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    listing = await session.list_tools()
        return [t.name for t in listing.tools]
