from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators import Pivot, analyze_timeframe, candles_to_frame, swing_points
from app.institutional import (
    analyze_institutional,
    anchored_vwap,
    dealing_range,
    equal_levels,
    fair_value_gaps,
    liquidity_sweeps,
    order_blocks,
)
from app.schemas import Candle, Timeframe


def _frame(rows: list[tuple[float, float, float, float]], volume: float = 1000.0) -> pd.DataFrame:
    return candles_to_frame([Candle(ts=i * 3_600_000, open=o, high=h, low=l, close=c, volume=volume) for i, (o, h, l, c) in enumerate(rows)])


def test_anchored_vwap_matches_manual() -> None:
    df = _frame([(10, 11, 9, 10), (10, 12, 10, 11), (11, 13, 11, 12)])
    vwap, std = anchored_vwap(df, 0)
    typical = [(11 + 9 + 10) / 3, (12 + 10 + 11) / 3, (13 + 11 + 12) / 3]
    assert vwap.iloc[-1] == sum(typical) / 3
    assert std.iloc[-1] >= 0


def test_anchored_vwap_without_volume_is_time_weighted() -> None:
    df = _frame([(10, 11, 9, 10), (10, 12, 10, 11)], volume=0.0)
    vwap, _ = anchored_vwap(df, 0)
    assert not np.isnan(vwap.iloc[-1])


def test_bullish_fvg_detected_and_unfilled() -> None:
    rows = [(10, 10.5, 9.5, 10)] * 5 + [(10, 10.6, 9.8, 10.5), (10.5, 12, 10.4, 11.8), (11.9, 12.5, 11.5, 12.2)] + [(12.2, 12.6, 12.0, 12.3)] * 3
    bull, bear = fair_value_gaps(_frame(rows))
    assert bull is not None and bull.low == 10.6 and bull.high == 11.5 and not bull.tested
    assert bear is None


def test_bullish_order_block_before_impulse() -> None:
    rows = [(10, 10.3, 9.8, 10.1)] * 10 + [(10.1, 10.2, 9.7, 9.8)] + [(9.8, 11.0, 9.8, 10.9), (10.9, 12.0, 10.8, 11.9), (11.9, 12.8, 11.8, 12.7)] + [(12.7, 12.9, 12.5, 12.6)] * 4
    bull, _ = order_blocks(_frame(rows), atr_value=0.5)
    assert bull is not None and bull.low == 9.7 and bull.high == 10.2 and not bull.tested


def test_liquidity_sweep_below_prior_swing_low() -> None:
    rows = [(10, 10.5, 9.5, 10)] * 3 + [(10, 10.2, 9.0, 9.3)] + [(9.3, 10.5, 9.2, 10.4)] * 3 + [(10.4, 10.6, 9.6, 10.2)] * 3 + [(10.2, 10.4, 8.8, 10.1)]
    df = _frame(rows)
    pivots = swing_points(df, left=2, right=2)
    assert any(p.kind == "low" and p.price == 9.0 for p in pivots)
    bull, bear = liquidity_sweeps(df, pivots)
    assert bull == 9.0 and bear is None


def test_equal_levels_and_dealing_range() -> None:
    pivots = [Pivot(1, 100.0, "high"), Pivot(5, 100.1, "high"), Pivot(3, 90.0, "low"), Pivot(7, 95.0, "low")]
    highs, lows = equal_levels(pivots, atr_value=1.0)
    assert highs == [100.05] and lows == []
    lo, hi, pos = dealing_range(pivots, close=95.0)
    assert (lo, hi) == (90.0, 100.1) and 45 < pos < 55


def test_analyze_timeframe_populates_institutional() -> None:
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0.1, 1.0, 300))
    rows = [(float(c - 0.2), float(c + 0.8), float(c - 0.8), float(c)) for c in close]
    ta = analyze_timeframe(_frame(rows), Timeframe.H4, "test")
    assert ta.institutional is not None
    ins = ta.institutional
    assert ins.vwap > 0 and "bars ago" in ins.vwap_anchor
    assert ins.vwap_lower_2 <= ins.vwap <= ins.vwap_upper_2
    full = analyze_institutional(_frame(rows), swing_points(_frame(rows)), ta.atr)
    assert full.vwap == ins.vwap
