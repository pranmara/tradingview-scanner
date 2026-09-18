from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from app.schemas import Candle, MarketStructure, TechnicalSnapshot, Timeframe, TimeframeAnalysis, VolumeProfile

MIN_BARS = 60


class InsufficientDataError(Exception):
    pass


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        raise InsufficientDataError("no candles")
    df = pd.DataFrame([c.model_dump() for c in candles])
    df = df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0.0, 100.0)
    return out.fillna(50.0)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(n).mean()
    std = close.rolling(n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    bbw = (upper - lower) / mid.replace(0.0, np.nan)
    return upper, mid, lower, bbw


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_s = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_s
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_s
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float
    kind: Literal["high", "low"]


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[Pivot]:
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    out: list[Pivot] = []
    for i in range(left, len(df) - right):
        wh = highs[i - left : i + right + 1]
        if highs[i] == wh.max() and int((wh == highs[i]).sum()) == 1:
            out.append(Pivot(i, float(highs[i]), "high"))
        wl = lows[i - left : i + right + 1]
        if lows[i] == wl.min() and int((wl == lows[i]).sum()) == 1:
            out.append(Pivot(i, float(lows[i]), "low"))
    return out


def market_structure(df: pd.DataFrame, pivots: list[Pivot], break_lookback: int = 5) -> MarketStructure:
    highs = [p for p in pivots if p.kind == "high"]
    lows = [p for p in pivots if p.kind == "low"]

    trend: Literal["bullish", "bearish", "ranging"] = "ranging"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price < lows[-2].price
        if hh and hl:
            trend = "bullish"
        elif lh and ll:
            trend = "bearish"

    last_high = highs[-1].price if highs else None
    last_low = lows[-1].price if lows else None
    recent = df["close"].iloc[-break_lookback:]
    broke_up = last_high is not None and bool((recent > last_high).any())
    broke_down = last_low is not None and bool((recent < last_low).any())

    return MarketStructure(
        trend=trend,
        msb_bullish=broke_up and trend != "bearish",
        choch_bullish=broke_up and trend == "bearish",
        msb_bearish=broke_down and trend != "bullish",
        choch_bearish=broke_down and trend == "bullish",
        last_swing_high=last_high,
        last_swing_low=last_low,
    )


def hidden_divergence(rsi_series: pd.Series, pivots: list[Pivot], n_bars: int, max_age: int = 30) -> tuple[bool, bool]:
    lows = [p for p in pivots if p.kind == "low"][-2:]
    highs = [p for p in pivots if p.kind == "high"][-2:]
    bull = bear = False
    if len(lows) == 2 and n_bars - 1 - lows[1].index <= max_age:
        r1, r2 = float(rsi_series.iloc[lows[0].index]), float(rsi_series.iloc[lows[1].index])
        bull = lows[1].price > lows[0].price and r2 < r1
    if len(highs) == 2 and n_bars - 1 - highs[1].index <= max_age:
        r1, r2 = float(rsi_series.iloc[highs[0].index]), float(rsi_series.iloc[highs[1].index])
        bear = highs[1].price < highs[0].price and r2 > r1
    return bull, bear


def volume_profile(df: pd.DataFrame, bins: int = 24, lookback: int = 100, value_area: float = 0.70) -> VolumeProfile | None:
    sub = df.iloc[-lookback:]
    lo, hi = float(sub["low"].min()), float(sub["high"].max())
    if hi <= lo or float(sub["volume"].sum()) <= 0:
        return None
    typical = ((sub["high"] + sub["low"] + sub["close"]) / 3.0).to_numpy()
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(typical, edges) - 1, 0, bins - 1)
    vol = np.zeros(bins)
    np.add.at(vol, idx, sub["volume"].to_numpy())
    total = float(vol.sum())
    poc_i = int(vol.argmax())
    lo_i = hi_i = poc_i
    acc = float(vol[poc_i])
    while acc < value_area * total and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1.0
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1.0
        if up >= down:
            hi_i += 1
            acc += float(vol[hi_i])
        else:
            lo_i -= 1
            acc += float(vol[lo_i])
    centers = (edges[:-1] + edges[1:]) / 2.0
    return VolumeProfile(poc=float(centers[poc_i]), vah=float(edges[hi_i + 1]), val=float(edges[lo_i]))


def ribbon_state(e20: float, e50: float, e200: float) -> Literal["bullish", "bearish", "mixed"]:
    if e20 > e50 > e200:
        return "bullish"
    if e20 < e50 < e200:
        return "bearish"
    return "mixed"


def return_pct(close: pd.Series, bars: int = 20) -> float | None:
    if len(close) <= bars:
        return None
    start = float(close.iloc[-1 - bars])
    if start == 0:
        return None
    return (float(close.iloc[-1]) / start - 1.0) * 100.0


def analysis_from_snapshot(snap: TechnicalSnapshot) -> TimeframeAnalysis:
    """Degraded analysis when no candle source is available but an indicator snapshot is."""
    if None in (snap.close, snap.ema20, snap.ema50, snap.ema200, snap.rsi, snap.atr):
        raise InsufficientDataError(f"{snap.timeframe.value}: snapshot missing core indicator fields")
    assert snap.close is not None and snap.ema20 is not None and snap.ema50 is not None
    assert snap.ema200 is not None and snap.rsi is not None and snap.atr is not None
    bbw = (snap.bb_upper - snap.bb_lower) / snap.close if snap.bb_upper is not None and snap.bb_lower is not None and snap.close else None
    return TimeframeAnalysis(
        timeframe=snap.timeframe,
        bars=0,
        source=f"{snap.source} (snapshot-only)",
        close=snap.close,
        ema20=snap.ema20,
        ema50=snap.ema50,
        ema200=snap.ema200,
        ribbon=ribbon_state(snap.ema20, snap.ema50, snap.ema200),
        rsi=snap.rsi,
        bbw=bbw if bbw is not None else 0.0,
        bb_squeeze=bbw is not None and bbw < 0.05,
        volume_ratio=0.0,
        volume_expansion=False,
        atr=snap.atr,
        structure=MarketStructure(trend="ranging"),
        hidden_div_bullish=False,
        hidden_div_bearish=False,
        volume_profile=None,
        return_20_pct=None,
        last_bar_bullish=snap.close >= snap.ema20,
        snapshot=snap,
        degraded=True,
    )


def analyze_timeframe(df: pd.DataFrame, timeframe: Timeframe, source: str) -> TimeframeAnalysis:
    n = len(df)
    if n < MIN_BARS:
        raise InsufficientDataError(f"{timeframe.value}: {n} bars < {MIN_BARS}")

    close = df["close"]
    e20, e50, e200 = ema(close, 20), ema(close, 50), ema(close, 200)
    rsi_s = rsi(close)
    adx_s = adx(df)
    _, _, _, bbw = bollinger(close)
    atr_s = atr(df)
    vol_sma20 = df["volume"].rolling(20).mean()
    pivots = swing_points(df)

    last_bbw = float(bbw.iloc[-1]) if not np.isnan(bbw.iloc[-1]) else 0.0
    last_vol_sma = float(vol_sma20.iloc[-1]) if not np.isnan(vol_sma20.iloc[-1]) else 0.0
    vol_ratio = float(df["volume"].iloc[-1]) / last_vol_sma if last_vol_sma > 0 else 0.0
    div_bull, div_bear = hidden_divergence(rsi_s, pivots, n)

    return TimeframeAnalysis(
        timeframe=timeframe,
        bars=n,
        source=source,
        close=float(close.iloc[-1]),
        ema20=float(e20.iloc[-1]),
        ema50=float(e50.iloc[-1]),
        ema200=float(e200.iloc[-1]),
        ribbon=ribbon_state(float(e20.iloc[-1]), float(e50.iloc[-1]), float(e200.iloc[-1])),
        rsi=float(rsi_s.iloc[-1]),
        adx=float(adx_s.iloc[-1]),
        bbw=last_bbw,
        bb_squeeze=last_bbw < 0.05,
        volume_ratio=vol_ratio,
        volume_expansion=vol_ratio > 1.5,
        atr=float(atr_s.iloc[-1]),
        structure=market_structure(df, pivots),
        hidden_div_bullish=div_bull,
        hidden_div_bearish=div_bear,
        volume_profile=volume_profile(df),
        return_20_pct=return_pct(close),
        last_bar_bullish=float(close.iloc[-1]) >= float(df["open"].iloc[-1]),
    )
