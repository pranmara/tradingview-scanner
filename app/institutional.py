"""Institutional execution footprints computed from OHLCV.

None of this is market making (that needs exchange-level infrastructure). These are the observable traces of how large
participants get filled: benchmarking to VWAP, taking resting liquidity beyond obvious swing points before reversing,
leaving price imbalances that later get revisited, and defending the last opposing candle before an impulsive move.
Each function is deterministic and uses only bars up to the last one, so the backtester can replay it without look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators import Pivot
from app.schemas import InstitutionalSignals, Zone


def anchored_vwap(df: pd.DataFrame, anchor: int) -> tuple[pd.Series, pd.Series]:
    """VWAP and volume-weighted standard deviation from bar `anchor` to the end."""
    sub = df.iloc[anchor:]
    typical = (sub["high"] + sub["low"] + sub["close"]) / 3.0
    vol = sub["volume"].astype(float)
    if float(vol.sum()) <= 0:
        vol = pd.Series(1.0, index=sub.index)  # feeds without volume degrade to a time-weighted average price
    cum_vol = vol.cumsum().replace(0.0, np.nan)
    vwap = (typical * vol).cumsum() / cum_vol
    variance = ((typical - vwap) ** 2 * vol).cumsum() / cum_vol
    return vwap, np.sqrt(variance.clip(lower=0.0))


def choose_anchor(df: pd.DataFrame, lookback: int = 120) -> tuple[int, str]:
    """Anchor at the most recent major extreme (highest high or lowest low) inside the lookback."""
    start = max(0, len(df) - lookback)
    window = df.iloc[start:]
    hi_idx = start + int(window["high"].to_numpy().argmax())
    lo_idx = start + int(window["low"].to_numpy().argmin())
    if hi_idx >= lo_idx:
        return hi_idx, "swing high"
    return lo_idx, "swing low"


def fair_value_gaps(df: pd.DataFrame, lookback: int = 60) -> tuple[Zone | None, Zone | None]:
    """Most recent unfilled bullish / bearish three-candle imbalance. A gap is 'filled' once price trades through it."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    bull: Zone | None = None
    bear: Zone | None = None
    for i in range(n - 2, max(1, n - lookback), -1):
        # candles i-1, i, i+1
        if bull is None and lows[i + 1] > highs[i - 1]:
            zone = Zone(low=float(highs[i - 1]), high=float(lows[i + 1]), index=i, kind="fvg")
            later_lows = lows[i + 2 :]
            if later_lows.size == 0 or later_lows.min() > zone.low:
                zone.tested = later_lows.size > 0 and bool(later_lows.min() <= zone.high)
                bull = zone
        if bear is None and highs[i + 1] < lows[i - 1]:
            zone = Zone(low=float(highs[i + 1]), high=float(lows[i - 1]), index=i, kind="fvg")
            later_highs = highs[i + 2 :]
            if later_highs.size == 0 or later_highs.max() < zone.high:
                zone.tested = later_highs.size > 0 and bool(later_highs.max() >= zone.low)
                bear = zone
        if bull is not None and bear is not None:
            break
    return bull, bear


def order_blocks(df: pd.DataFrame, atr_value: float, lookback: int = 60, impulse_atr: float = 1.5) -> tuple[Zone | None, Zone | None]:
    """Last opposing candle before an impulsive move of >= impulse_atr ATRs within the next 3 bars, not yet invalidated."""
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    bull: Zone | None = None
    bear: Zone | None = None
    if atr_value <= 0:
        return None, None
    for i in range(n - 4, max(0, n - lookback), -1):
        nxt_high, nxt_low = highs[i + 1 : i + 4].max(), lows[i + 1 : i + 4].min()
        if bull is None and closes[i] < opens[i] and nxt_high - closes[i] >= impulse_atr * atr_value and closes[i + 3] > highs[i]:
            later_lows = lows[i + 4 :]
            if later_lows.size == 0 or later_lows.min() >= lows[i]:  # not invalidated
                bull = Zone(low=float(lows[i]), high=float(highs[i]), index=i, kind="order_block",
                            tested=later_lows.size > 0 and bool(later_lows.min() <= highs[i]))
        if bear is None and closes[i] > opens[i] and closes[i] - nxt_low >= impulse_atr * atr_value and closes[i + 3] < lows[i]:
            later_highs = highs[i + 4 :]
            if later_highs.size == 0 or later_highs.max() <= highs[i]:
                bear = Zone(low=float(lows[i]), high=float(highs[i]), index=i, kind="order_block",
                            tested=later_highs.size > 0 and bool(later_highs.max() >= lows[i]))
        if bull is not None and bear is not None:
            break
    return bull, bear


def liquidity_sweeps(df: pd.DataFrame, pivots: list[Pivot], lookback: int = 8) -> tuple[float | None, float | None]:
    """A bar that trades beyond a prior swing point but closes back inside = resting stops taken, then rejected."""
    closes, highs, lows = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    bull_level: float | None = None
    bear_level: float | None = None
    for i in range(n - 1, max(0, n - 1 - lookback), -1):
        prior_lows = [p.price for p in pivots if p.kind == "low" and p.index < i - 1]
        prior_highs = [p.price for p in pivots if p.kind == "high" and p.index < i - 1]
        if bull_level is None and prior_lows:
            level = prior_lows[-1]
            if lows[i] < level and closes[i] > level:
                bull_level = float(level)
        if bear_level is None and prior_highs:
            level = prior_highs[-1]
            if highs[i] > level and closes[i] < level:
                bear_level = float(level)
        if bull_level is not None and bear_level is not None:
            break
    return bull_level, bear_level


def equal_levels(pivots: list[Pivot], atr_value: float, tolerance_atr: float = 0.2, recent: int = 8) -> tuple[list[float], list[float]]:
    """Clusters of swing highs / lows within tolerance = liquidity pools (targets for sweeps)."""
    def cluster(points: list[float]) -> list[float]:
        out: list[float] = []
        pts = points[-recent:]
        for i, a in enumerate(pts):
            for b in pts[i + 1 :]:
                if abs(a - b) <= tolerance_atr * atr_value:
                    level = round((a + b) / 2.0, 8)
                    if not any(abs(level - o) <= tolerance_atr * atr_value for o in out):
                        out.append(level)
        return sorted(out)

    if atr_value <= 0:
        return [], []
    return (cluster([p.price for p in pivots if p.kind == "high"]), cluster([p.price for p in pivots if p.kind == "low"]))


def dealing_range(pivots: list[Pivot], close: float, recent: int = 6) -> tuple[float | None, float | None, float | None]:
    highs = [p.price for p in pivots if p.kind == "high"][-recent:]
    lows = [p.price for p in pivots if p.kind == "low"][-recent:]
    if not highs or not lows:
        return None, None, None
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        return None, None, None
    return lo, hi, (close - lo) / (hi - lo) * 100.0


def analyze_institutional(df: pd.DataFrame, pivots: list[Pivot], atr_value: float) -> InstitutionalSignals:
    anchor, anchor_label = choose_anchor(df)
    vwap, std = anchored_vwap(df, anchor)
    close = float(df["close"].iloc[-1])
    v_now = float(vwap.iloc[-1])
    v_prev = float(vwap.iloc[-6]) if len(vwap) > 6 else float(vwap.iloc[0])
    s_now = float(std.iloc[-1]) if not np.isnan(std.iloc[-1]) else 0.0
    bull_fvg, bear_fvg = fair_value_gaps(df)
    bull_ob, bear_ob = order_blocks(df, atr_value)
    sweep_bull, sweep_bear = liquidity_sweeps(df, pivots)
    eq_highs, eq_lows = equal_levels(pivots, atr_value)
    r_lo, r_hi, r_pos = dealing_range(pivots, close)
    return InstitutionalSignals(
        vwap=v_now,
        vwap_anchor=f"{anchor_label} {len(df) - 1 - anchor} bars ago",
        vwap_slope_pct=(v_now / v_prev - 1.0) * 100.0 if v_prev else 0.0,
        vwap_upper_2=v_now + 2.0 * s_now,
        vwap_lower_2=v_now - 2.0 * s_now,
        price_vs_vwap_pct=(close / v_now - 1.0) * 100.0 if v_now else 0.0,
        sweep_bullish_level=sweep_bull,
        sweep_bearish_level=sweep_bear,
        fvg_bullish=bull_fvg,
        fvg_bearish=bear_fvg,
        order_block_bullish=bull_ob,
        order_block_bearish=bear_ob,
        equal_highs=[lvl for lvl in eq_highs if lvl > close],
        equal_lows=[lvl for lvl in eq_lows if lvl < close],
        range_low=r_lo,
        range_high=r_hi,
        range_position_pct=r_pos,
    )
