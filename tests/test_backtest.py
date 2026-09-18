from __future__ import annotations

import numpy as np
import pandas as pd

from app.asset_classifier import classify
from app.backtest import Backtester, _first_hit, compute_calibration, compute_metrics, Observation, Trade
from app.config import Settings
from app.decision_engine import DecisionEngine
from app.indicators import candles_to_frame
from app.schemas import Candle, Side, Timeframe


def _synthetic(n: int, seed: int = 3, drift: float = 0.15) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(drift, 1.0, n)) + 4 * np.sin(np.arange(n) / 9)
    close = np.maximum(close, 5.0)
    candles = [
        Candle(ts=i * 4 * 3_600_000, open=float(c - rng.normal(0, 0.3)), high=float(c + abs(rng.normal(0.8, 0.4))),
               low=float(c - abs(rng.normal(0.8, 0.4))), close=float(c), volume=float(1000 + rng.integers(0, 800)))
        for i, c in enumerate(close)
    ]
    return candles_to_frame(candles)


def test_first_hit_prefers_stop_on_same_bar() -> None:
    df = pd.DataFrame({"ts": [0, 1, 2], "open": [10, 10, 10], "high": [10, 15, 10], "low": [10, 5, 10], "close": [10, 10, 10]})
    i, reason, px = _first_hit(df, 1, Side.BUY, sl=8.0, targets=[12.0], max_bars=5)
    assert (i, reason, px) == (1, "sl", 8.0)


def test_first_hit_time_stop() -> None:
    df = pd.DataFrame({"ts": range(10), "open": [10] * 10, "high": [10.5] * 10, "low": [9.5] * 10, "close": [10.1] * 10})
    i, reason, px = _first_hit(df, 2, Side.SELL, sl=12.0, targets=[8.0], max_bars=4)
    assert reason == "time" and i == 5 and px == 10.1


def test_backtester_runs_without_lookahead(settings: Settings) -> None:
    df = _synthetic(520)
    engine = DecisionEngine(settings)
    bt = Backtester(engine, classify("BTCUSDT"), Timeframe.H4, {Timeframe.H4: df}, min_score=0, step=5, time_stop_bars=20)
    result = bt.run()
    assert result.evaluated > 0
    assert sum(result.signal_counts.values()) == result.evaluated
    for t in result.trades:
        assert t.exit_index >= t.entry_index
        assert t.entry_index > 210
    if result.trades:
        assert set(result.metrics) >= {"trades", "win_rate_pct", "avg_r", "profit_factor", "max_drawdown_r"}
    assert len(result.calibration) == 4


def test_metrics_and_calibration() -> None:
    trades = [Trade("BUY", 0, 0, 1, 0.9, 1.1, 1.2, 1.3, 80, r_multiple=r, bars_held=3) for r in (2.0, -1.0, 1.5, -1.0)]
    m = compute_metrics(trades)
    assert m["trades"] == 4 and m["win_rate_pct"] == 50.0 and m["total_r"] == 1.5 and m["profit_factor"] == 1.75
    obs = [Observation(0, 85, "BUY", "BUY", hit=True), Observation(1, 85, "BUY", "BUY", hit=False), Observation(2, 30, None, "NEUTRAL", hit=None)]
    cal = compute_calibration(obs)
    assert cal[-1]["samples"] == 2 and cal[-1]["hit_2_5r_pct"] == 50.0
