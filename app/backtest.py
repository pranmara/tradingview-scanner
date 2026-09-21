"""Walk-forward backtest that replays candles through the live indicator + decision engine.

Usage:
    python -m app.backtest BTCUSDT --tf 4h --bars 1500
    python -m app.backtest AAPL --tf 1d --bars 800 --exit-mode tp2 --min-score 70
    python -m app.backtest --journal data/signals.jsonl        # forward-test live signals

What it can and cannot validate:
  * Uses the exact TA path (indicators.py + decision_engine.py) so bucket logic, filters and levels are the ones
    running in production. On-chain (Nansen) and Pine alerts have no history, so those buckets are absent and the
    score is renormalised over the available 70 points — identical to a live scan without a Nansen key.
  * Signals are evaluated on the CLOSE of bar i using only bars <= i (no look-ahead); entries fill at the OPEN of
    bar i+1 with slippage; if a bar touches both SL and a target, SL is assumed first (conservative).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from app.asset_classifier import AssetInfo, classify
from app.decision_engine import TP_MULTIPLES, DecisionEngine, ScanInputs
from app.indicators import MIN_BARS, InsufficientDataError, analyze_timeframe, candles_to_frame
from app.schemas import Side, Signal, Timeframe, TimeframeAnalysis
from app.timeframes import BAR_MS, parse_timeframe

if TYPE_CHECKING:  # heavy client imports stay deferred so the CLI starts fast
    import httpx

    from app.clients.market_data import CompositeMarketDataProvider
    from app.config import Settings

ExitMode = Literal["tp2", "scaled"]
SCORE_BUCKETS = ((0, 40), (40, 60), (60, 80), (80, 101))


@dataclass
class Trade:
    side: str
    entry_index: int
    entry_ts: int
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    score: float
    exit_index: int = 0
    exit_ts: int = 0
    exit_reason: str = ""
    r_multiple: float = 0.0
    bars_held: int = 0


@dataclass
class Observation:
    """Every evaluated bar, used for score calibration regardless of whether a trade was taken."""

    index: int
    score: float
    direction: str | None
    signal: str
    hit: bool | None = None  # reached 2.5R before -1R within the time stop


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    bars: int
    evaluated: int
    trades: list[Trade]
    observations: list[Observation]
    metrics: dict[str, float] = field(default_factory=dict)
    calibration: list[dict[str, float]] = field(default_factory=list)
    signal_counts: dict[str, int] = field(default_factory=dict)


def _first_hit(df: pd.DataFrame, start: int, side: Side, sl: float, targets: list[float], max_bars: int) -> tuple[int, str, float]:
    """Return (bar index, reason, price) of the first SL/target touch after `start`, or a time stop."""
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    end = min(len(df), start + max_bars)
    for i in range(start, end):
        if side is Side.BUY:
            if lows[i] <= sl:
                return i, "sl", sl
            for k, t in enumerate(targets):
                if highs[i] >= t:
                    return i, f"tp{k + 1}", t
        else:
            if highs[i] >= sl:
                return i, "sl", sl
            for k, t in enumerate(targets):
                if lows[i] <= t:
                    return i, f"tp{k + 1}", t
    last = end - 1
    return last, "time", float(closes[last])


class Backtester:
    def __init__(
        self,
        engine: DecisionEngine,
        asset: AssetInfo,
        primary: Timeframe,
        frames: dict[Timeframe, pd.DataFrame],
        *,
        min_score: float | None = None,
        exit_mode: ExitMode = "scaled",
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        time_stop_bars: int = 40,
        window: int = 300,
        warmup: int = 210,
        step: int = 1,
    ) -> None:
        self._engine = engine
        self._asset = asset
        self._primary = primary
        self._frames = frames
        self._min_score = min_score
        self._exit_mode: ExitMode = exit_mode
        self._cost_frac = (2 * fee_bps + slippage_bps) / 10_000.0
        self._slip = slippage_bps / 10_000.0
        self._time_stop = time_stop_bars
        self._window = window
        self._warmup = warmup
        self._step = max(1, step)

    def _analyses_at(self, ts: int) -> dict[Timeframe, TimeframeAnalysis]:
        out: dict[Timeframe, TimeframeAnalysis] = {}
        for tf, f in self._frames.items():
            end = int(np.searchsorted(f["ts"].to_numpy(), ts, side="right"))
            sub = f.iloc[max(0, end - self._window) : end]
            if len(sub) < MIN_BARS:
                continue
            try:
                out[tf] = analyze_timeframe(sub.reset_index(drop=True), tf, "backtest")
            except InsufficientDataError:
                continue
        return out

    def _simulate(self, df: pd.DataFrame, i: int, side: Side, entry: float, sl: float, tps: list[float], score: float) -> Trade:
        risk = abs(entry - sl)
        trade = Trade(side.value, i, int(df["ts"].iloc[i]), entry, sl, tps[0], tps[1], tps[2], score)
        if self._exit_mode == "tp2":
            j, reason, px = _first_hit(df, i, side, sl, [tps[1]], self._time_stop)
            r = (px - entry) / risk if side is Side.BUY else (entry - px) / risk
        else:
            # 40% at TP1 then SL -> breakeven, 30% at TP2 then SL -> TP1, 30% at TP3 (or time/SL).
            remaining, r, cur_sl, start, j, reason = 1.0, 0.0, sl, i, i, "time"
            for k, frac in ((0, 0.4), (1, 0.3), (2, 0.3)):
                budget = max(1, self._time_stop - (start - i))
                j, hit, px = _first_hit(df, start, side, cur_sl, [tps[k]], budget)
                leg_r = (px - entry) / risk if side is Side.BUY else (entry - px) / risk
                if hit == "tp1":  # single-target call: "tp1" means target k was reached
                    r += leg_r * frac
                    remaining -= frac
                    cur_sl = entry if k == 0 else tps[k - 1]
                    start = j
                    reason = f"tp{k + 1}"
                    continue
                r += leg_r * remaining
                reason = "be" if (hit == "sl" and k > 0) else hit
                break
        r -= self._cost_frac * entry / risk
        trade.exit_index, trade.exit_ts, trade.exit_reason = j, int(df["ts"].iloc[j]), reason
        trade.r_multiple, trade.bars_held = r, j - i
        return trade

    def run(self) -> BacktestResult:
        df = self._frames[self._primary].reset_index(drop=True)
        n = len(df)
        trades: list[Trade] = []
        observations: list[Observation] = []
        counts: dict[str, int] = {s.value: 0 for s in Signal}
        i = self._warmup
        while i < n - 1:
            ts = int(df["ts"].iloc[i])
            analyses = self._analyses_at(ts)
            if self._primary not in analyses:
                i += self._step
                continue
            report = self._engine.evaluate(ScanInputs(self._asset, self._primary, analyses, now_ms=ts))
            counts[report.signal.value] += 1
            obs = Observation(i, report.score, report.direction.value if report.direction else None, report.signal.value)
            lv = report.levels
            if lv is not None and report.direction is not None:
                risk = lv.risk_per_unit
                tp = lv.entry + TP_MULTIPLES[1] * risk if report.direction is Side.BUY else lv.entry - TP_MULTIPLES[1] * risk
                _, reason, _ = _first_hit(df, i + 1, report.direction, lv.stop_loss, [tp], self._time_stop)
                obs.hit = reason == "tp1"  # single target list, so "tp1" == our 2.5R target
            observations.append(obs)

            take = report.signal in (Signal.BUY, Signal.SELL)
            if self._min_score is not None and lv is not None and report.direction is not None:
                take = report.score >= self._min_score and not report.vetoes and lv.effective_rrr >= TP_MULTIPLES[1]
            if take and lv is not None and report.direction is not None:
                side = report.direction
                fill = float(df["open"].iloc[i + 1])
                fill *= (1 + self._slip) if side is Side.BUY else (1 - self._slip)
                risk = lv.risk_per_unit
                tps = [fill + k * risk for k in TP_MULTIPLES] if side is Side.BUY else [fill - k * risk for k in TP_MULTIPLES]
                sl = fill - risk if side is Side.BUY else fill + risk
                trade = self._simulate(df, i + 1, side, fill, sl, tps, report.score)
                trades.append(trade)
                i = trade.exit_index + 1
                continue
            i += self._step

        result = BacktestResult(self._asset.symbol, self._primary.value, n, len(observations), trades, observations, signal_counts=counts)
        result.metrics = compute_metrics(trades)
        result.calibration = compute_calibration(observations)
        return result


def compute_metrics(trades: list[Trade]) -> dict[str, float]:
    if not trades:
        return {"trades": 0}
    rs = np.array([t.r_multiple for t in trades])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    equity = np.cumsum(rs)
    drawdown = equity - np.maximum.accumulate(equity)
    return {
        "trades": len(trades),
        "win_rate_pct": round(float(len(wins) / len(rs) * 100), 1),
        "avg_r": round(float(rs.mean()), 3),
        "median_r": round(float(np.median(rs)), 3),
        "avg_win_r": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if len(losses) and losses.sum() != 0 else math.inf,
        "total_r": round(float(rs.sum()), 2),
        "max_drawdown_r": round(float(drawdown.min()), 2),
        "sharpe_per_trade": round(float(rs.mean() / rs.std()), 3) if rs.std() > 0 else 0.0,
        "avg_bars_held": round(float(np.mean([t.bars_held for t in trades])), 1),
    }


def compute_calibration(observations: list[Observation]) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for lo, hi in SCORE_BUCKETS:
        obs = [o for o in observations if lo <= o.score < hi and o.hit is not None]
        hits = sum(1 for o in obs if o.hit)
        rows.append({"score_from": lo, "score_to": min(hi, 100), "samples": len(obs),
                     "hit_2_5r_pct": round(hits / len(obs) * 100, 1) if obs else float("nan")})
    return rows


def format_result(r: BacktestResult) -> str:
    lines = [f"== {r.symbol} {r.timeframe} — {r.bars} bars, {r.evaluated} evaluations ==",
             "signals: " + ", ".join(f"{k}={v}" for k, v in r.signal_counts.items()), "", "Trade metrics:"]
    lines += [f"  {k:<18} {v}" for k, v in r.metrics.items()]
    lines += ["", "Score calibration (P[+2.5R before -1R] by score bucket — should rise with score):"]
    for row in r.calibration:
        pct = row["hit_2_5r_pct"]
        lines.append(f"  {int(row['score_from']):>3}-{int(row['score_to']):<3}  n={int(row['samples']):<5} hit={'n/a' if math.isnan(pct) else f'{pct:.1f}%'}")
    breakeven = 100.0 / (1.0 + TP_MULTIPLES[1])
    lines.append(f"  (breakeven hit-rate at 2.5R ≈ {breakeven:.0f}% before costs)")
    if r.trades:
        lines += ["", "Last trades:"]
        for t in r.trades[-8:]:
            lines.append(f"  {t.side:<4} score={t.score:>5.1f} entry={t.entry:.4g} exit={t.exit_reason:<4} R={t.r_multiple:+.2f} bars={t.bars_held}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- journal forward-test
def evaluate_journal(path: str, frames_for: dict[tuple[str, str], pd.DataFrame], time_stop_bars: int) -> list[dict[str, object]]:
    """Replay journaled live signals against candles that arrived afterwards."""
    rows: list[dict[str, object]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("signal") not in ("BUY", "SELL") or rec.get("entry") is None:
            continue
        df = frames_for.get((rec["symbol"], rec["timeframe"]))
        if df is None:
            continue
        start = int(np.searchsorted(df["ts"].to_numpy(), rec["ts_ms"], side="right"))
        if start >= len(df):
            rows.append({**rec, "outcome": "pending"})
            continue
        side = Side(rec["signal"])
        risk = abs(rec["entry"] - rec["stop_loss"])
        # Single target at TP2, matching management_plan(). Passing [tp1, tp2, tp3] exited at whichever target
        # was touched first — always TP1 — which was neither the old scale-out nor the current plan.
        j, reason, px = _first_hit(df, start, side, rec["stop_loss"], [rec["tp2"]], time_stop_bars)
        if reason == "time" and start + time_stop_bars > len(df):
            # The data ran out, not the time stop: this trade is still open and must not be scored at today's close.
            rows.append({**rec, "outcome": "pending"})
            continue
        reason = "tp2" if reason == "tp1" else reason
        r = (px - rec["entry"]) / risk if side is Side.BUY else (rec["entry"] - px) / risk
        rows.append({**rec, "outcome": reason, "r_multiple": round(r, 2), "bars": j - start})
    return rows


# ---------------------------------------------------------------------------- CLI
def backtest_provider(http: httpx.AsyncClient, settings: Settings) -> CompositeMarketDataProvider:
    """Backtests use the public-only provider: reproducible bars, and the same Binance/Bybit/Yahoo chain a live
    scan falls back to, so a Bybit-only token can be tested."""
    from app.clients.market_data import public_market_provider

    return public_market_provider(http, settings)


async def _load_frames(asset: AssetInfo, primary: Timeframe, bars: int) -> dict[Timeframe, pd.DataFrame]:
    import httpx

    from app.config import get_settings

    s = get_settings()
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "tradingview-scanner-backtest/1.0"}, follow_redirects=True) as http:
        market = backtest_provider(http, s)
        frames: dict[Timeframe, pd.DataFrame] = {}
        span_ms = None
        for tf in dict.fromkeys([primary, Timeframe.H1, Timeframe.H4, Timeframe.D1]):
            want = bars if tf == primary else (span_ms // BAR_MS[tf] + 300 if span_ms else bars)
            try:
                ohlcv = await market.get_ohlcv_history(asset, tf, min(int(want), 5000))
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {tf.value}: {exc}", file=sys.stderr)
                continue
            frames[tf] = candles_to_frame(ohlcv.candles)
            if tf == primary:
                span_ms = int(frames[tf]["ts"].iloc[-1] - frames[tf]["ts"].iloc[0])
        return frames


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward backtest of the confluence scanner")
    parser.add_argument("symbol", nargs="?", help="e.g. BTCUSDT or AAPL")
    parser.add_argument("--tf", default="4h")
    parser.add_argument("--bars", type=int, default=1500)
    parser.add_argument("--min-score", type=float, default=None, help="Override MIN_SIGNAL_SCORE for the trade filter")
    parser.add_argument("--exit-mode", choices=["tp2", "scaled"], default="tp2",
                    help="tp2 (default) closes fully at 2.5R; scaled takes 40/30/30 at TP1/TP2/TP3 and "
                         "moved SL to breakeven, which measured ~0.2R/trade worse")
    parser.add_argument("--fee-bps", type=float, default=None)
    parser.add_argument("--slippage-bps", type=float, default=None)
    parser.add_argument("--time-stop", type=int, default=None)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--json", dest="json_out", default=None, help="Write full result to this path")
    parser.add_argument("--journal", default=None, help="Forward-test a signals.jsonl file instead of backtesting")
    args = parser.parse_args(argv)

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "backtest")
    os.environ.setdefault("TV_WEBHOOK_SECRET", "backtest")
    from app.config import get_settings
    from app.custom_indicators import CustomIndicatorRules

    s = get_settings()
    engine = DecisionEngine(s, CustomIndicatorRules.load(s.custom_indicators_path))
    time_stop = args.time_stop or s.backtest_time_stop_bars

    if args.journal:
        records = [json.loads(l) for l in Path(args.journal).read_text(encoding="utf-8").splitlines() if l.strip()]
        keys = {(r["symbol"], r["timeframe"]) for r in records if r.get("signal") in ("BUY", "SELL")}
        frames_for: dict[tuple[str, str], pd.DataFrame] = {}
        for sym, tf in keys:
            tfe = parse_timeframe(tf)
            if tfe is None:
                continue
            frames = asyncio.run(_load_frames(classify(sym), tfe, 1000))
            if tfe in frames:
                frames_for[(sym, tf)] = frames[tfe]
        rows = evaluate_journal(args.journal, frames_for, time_stop)
        closed = [r for r in rows if r["outcome"] != "pending"]
        print(f"journal: {len(rows)} signals, {len(closed)} resolved")
        for r in rows:
            print(f"  {r['symbol']:<10} {r['timeframe']:<3} {r['signal']:<4} score={r['score']:>5.1f} -> {r['outcome']:<7} R={r.get('r_multiple', '')}")
        if closed:
            rs = [float(r["r_multiple"]) for r in closed]
            print(f"  avg R={np.mean(rs):+.2f}  win%={np.mean([x > 0 for x in rs]) * 100:.0f}  total R={sum(rs):+.2f}")
        return 0

    if not args.symbol:
        parser.error("symbol is required unless --journal is given")
    tf = parse_timeframe(args.tf)
    if tf is None:
        parser.error("invalid --tf")
    asset = classify(args.symbol)
    print(f"loading {asset.symbol} ({asset.asset_class.value}) …")
    frames = asyncio.run(_load_frames(asset, tf, args.bars))
    if tf not in frames:
        print("primary timeframe data unavailable", file=sys.stderr)
        return 1
    bt = Backtester(
        engine, asset, tf, frames, min_score=args.min_score, exit_mode=args.exit_mode,
        fee_bps=args.fee_bps if args.fee_bps is not None else s.backtest_fee_bps,
        slippage_bps=args.slippage_bps if args.slippage_bps is not None else s.backtest_slippage_bps,
        time_stop_bars=time_stop, step=args.step,
    )
    result = bt.run()
    print(format_result(result))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
