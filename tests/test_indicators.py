from __future__ import annotations

import math

import numpy as np
import pandas as pd

import pytest

from app.indicators import (
    InsufficientDataError,
    Pivot,
    analysis_from_snapshot,
    analyze_timeframe,
    candles_to_frame,
    hidden_divergence,
    market_structure,
    rsi,
    swing_points,
    volume_profile,
)
from app.schemas import Candle, TechnicalSnapshot, Timeframe


def _frame(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    vols = volumes or [1000.0] * len(closes)
    candles = [
        Candle(ts=i * 3_600_000, open=c * 0.998, high=c * 1.01, low=c * 0.99, close=c, volume=v)
        for i, (c, v) in enumerate(zip(closes, vols, strict=True))
    ]
    return candles_to_frame(candles)


def _uptrend(n: int = 300) -> list[float]:
    rng = np.random.default_rng(7)
    base = 100 + np.arange(n) * 0.5
    wave = 3 * np.sin(np.arange(n) / 6)
    return list(base + wave + rng.normal(0, 0.2, n))


def test_rsi_bounds() -> None:
    df = _frame(_uptrend())
    r = rsi(df["close"])
    assert r.between(0, 100).all()
    assert r.iloc[-1] > 50


def test_analyze_uptrend_ribbon_bullish() -> None:
    ta = analyze_timeframe(_frame(_uptrend()), Timeframe.H1, "test")
    assert ta.ribbon == "bullish"
    assert ta.ema20 > ta.ema50 > ta.ema200
    assert ta.atr > 0
    assert ta.volume_profile is not None
    assert ta.volume_profile.val <= ta.volume_profile.poc <= ta.volume_profile.vah


def test_swing_points_and_structure_bullish() -> None:
    df = _frame(_uptrend())
    pivots = swing_points(df)
    assert any(p.kind == "high" for p in pivots) and any(p.kind == "low" for p in pivots)
    ms = market_structure(df, pivots)
    assert ms.trend in {"bullish", "ranging"}
    assert ms.last_swing_low is not None and ms.last_swing_high is not None


def test_market_structure_bullish_msb() -> None:
    closes = [100, 105, 102, 108, 104, 111, 106, 113, 109, 116, 112, 119, 115, 122, 118, 125, 121, 128, 124, 131, 140, 141, 142]
    df = _frame([float(c) for c in closes])
    pivots = swing_points(df, left=1, right=1)
    ms = market_structure(df, pivots, break_lookback=3)
    assert ms.trend == "bullish"
    assert ms.msb_bullish and not ms.choch_bearish


def test_hidden_bullish_divergence() -> None:
    n = 60
    rsi_series = pd.Series(np.linspace(40, 60, n))
    rsi_series.iloc[20] = 45.0
    rsi_series.iloc[45] = 35.0
    pivots = [Pivot(20, 100.0, "low"), Pivot(45, 105.0, "low")]
    bull, bear = hidden_divergence(rsi_series, pivots, n)
    assert bull and not bear


def test_analysis_from_snapshot_degraded() -> None:
    snap = TechnicalSnapshot(symbol="NASDAQ:AAPL", timeframe=Timeframe.D1, source="tv-scanner", close=200.0,
                             ema20=198.0, ema50=195.0, ema200=180.0, rsi=61.0, atr=3.0, bb_upper=205.0, bb_lower=195.0)
    ta = analysis_from_snapshot(snap)
    assert ta.degraded and ta.bars == 0
    assert ta.ribbon == "bullish"
    assert ta.bbw == pytest.approx(0.05) and not ta.bb_squeeze
    assert ta.structure.last_swing_low is None
    with pytest.raises(InsufficientDataError):
        analysis_from_snapshot(TechnicalSnapshot(symbol="X", timeframe=Timeframe.D1, source="s", close=1.0))


def test_volume_profile_poc_in_range() -> None:
    df = _frame(_uptrend(150))
    vp = volume_profile(df)
    assert vp is not None
    assert df["low"].min() <= vp.poc <= df["high"].max()
    assert not math.isnan(vp.vah)
