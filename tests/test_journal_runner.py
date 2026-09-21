from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pandas as pd
import pytest

from app.backtest import evaluate_journal
from app.config import Settings
from app.execution_router import ExecutionRouter
from app.journal_runner import (
    MIN_RESOLVED_TO_JUDGE,
    ForwardReport,
    JournalRunner,
    auc_with_ci,
    format_digest,
    forward_report,
    is_digest_slot,
    next_slot_ms,
    resolve,
    weekly_sample,
)
from app.signal_journal import SignalJournal, read_journal

H4 = 4 * 3_600_000
DAY = 86_400_000


def _ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def _frame(start_ms: int, closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "ts": [start_ms + i * H4 for i in range(n)],
        "open": closes, "close": closes,
        "high": highs or closes, "low": lows or closes, "volume": [1.0] * n,
    })


def _rec(ts: int, direction: str = "BUY", score: float = 60.0, signal: str = "WATCH", source: str = "scheduled",
         symbol: str = "BTCUSDT", entry: float = 100.0, sl: float = 90.0, tp2: float = 125.0) -> dict:
    return {"ts_ms": ts, "source": source, "symbol": symbol, "timeframe": "4h", "signal": signal,
            "direction": direction, "score": score, "entry": entry, "stop_loss": sl, "tp1": 115.0,
            "tp2": tp2, "tp3": 140.0}


# ------------------------------------------------------------------ safety: the scheduler cannot trade
def test_the_journal_settings_copy_cannot_dispatch_an_order(settings: Settings) -> None:
    """main.py builds the journal orchestrator from this exact copy; dispatch must refuse on it."""
    from tests.test_decision_engine import _all_bullish  # a full bullish setup that scores a BUY
    from app.asset_classifier import classify
    from app.decision_engine import DecisionEngine, ScanInputs

    live = settings.model_copy(update={"execution_enabled": True, "dry_run": False,
                                       "execution_webhook_url": "https://exec.invalid/hook"})
    jset = live.model_copy(update={"execution_enabled": False, "nansen_mode": "off"})
    from app.schemas import Signal, Timeframe

    report = DecisionEngine(jset).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4,
                                                      analyses=_all_bullish()))
    assert report.signal is Signal.BUY   # the case that matters: a genuine BUY reaching dispatch

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the journal must never reach the execution webhook")

    outcome = asyncio.run(ExecutionRouter(jset, httpx.AsyncClient(transport=httpx.MockTransport(refuse))).dispatch(report))
    assert outcome == "disabled"
    assert jset.nansen_mode == "off"


# ------------------------------------------------------------------ schedule
def test_slots_sit_on_a_utc_grid_after_the_bar_closes():
    now = _ms(2026, 9, 21, 9, 30)
    assert next_slot_ms(now, 4, 5) == _ms(2026, 9, 21, 12, 5)


def test_a_slot_is_always_strictly_in_the_future():
    exactly_on_slot = _ms(2026, 9, 21, 12, 5)
    assert next_slot_ms(exactly_on_slot, 4, 5) == _ms(2026, 9, 21, 16, 5)


def test_just_before_the_offset_still_catches_this_bar():
    assert next_slot_ms(_ms(2026, 9, 21, 12, 2), 4, 5) == _ms(2026, 9, 21, 12, 5)


def test_exactly_one_digest_slot_per_week():
    monday = _ms(2026, 9, 21)                      # 2026-09-21 is a Monday
    week = [monday + i * H4 + 5 * 60_000 for i in range(7 * 6)]
    hits = [s for s in week if is_digest_slot(s, 0, 8, 4)]
    assert len(hits) == 1
    assert datetime.fromtimestamp(hits[0] / 1000, UTC).hour == 8


def test_interval_must_tile_the_day():
    with pytest.raises(ValueError):
        Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None,  # type: ignore[call-arg]
                 journal_interval_hours=5)


# ------------------------------------------------------------------ statistics
def test_auc_is_one_for_perfect_ranking():
    a, lo, hi = auc_with_ci([1, 2, 3, 4], [False, False, True, True])
    assert a == 1.0 and hi == 1.0


def test_auc_is_zero_for_inverted_ranking():
    assert auc_with_ci([4, 3, 2, 1], [False, False, True, True])[0] == 0.0


def test_a_constant_predictor_scores_exactly_half():
    assert auc_with_ci([5, 5, 5, 5], [True, False, True, False])[0] == 0.5


def test_auc_needs_both_outcomes():
    assert auc_with_ci([1, 2, 3], [True, True, True]) is None


def test_the_interval_narrows_as_the_sample_grows():
    small = auc_with_ci(list(range(20)), [i % 3 == 0 for i in range(20)])
    large = auc_with_ci(list(range(400)), [i % 3 == 0 for i in range(400)])
    assert (large[2] - large[1]) < (small[2] - small[1])


# ------------------------------------------------------------------ sampling
def test_weekly_sample_keeps_the_first_record_per_symbol_per_week():
    mon = _ms(2026, 9, 21, 0, 5)
    recs = [_rec(mon), _rec(mon + H4), _rec(mon + 2 * DAY), _rec(mon, symbol="ETHUSDT"),
            _rec(mon + 7 * DAY)]
    out = weekly_sample(recs)
    assert len(out) == 3
    assert {(r["symbol"], r["ts_ms"]) for r in out} == {("BTCUSDT", mon), ("ETHUSDT", mon), ("BTCUSDT", mon + 7 * DAY)}


# ------------------------------------------------------------------ resolution
def test_tp2_hit_resolves_as_a_win():
    start = _ms(2026, 1, 1)
    df = _frame(start, [100] * 60, highs=[100, 110, 130] + [100] * 57)
    outcome, r = resolve(_rec(start), df, 40)
    assert outcome == "tp2" and r == pytest.approx(2.5)


def test_stop_hit_resolves_as_a_loss():
    start = _ms(2026, 1, 1)
    df = _frame(start, [100] * 60, lows=[100, 95, 85] + [100] * 57)
    outcome, r = resolve(_rec(start), df, 40)
    assert outcome == "sl" and r == pytest.approx(-1.0)


def test_a_sell_is_scored_from_the_short_side():
    start = _ms(2026, 1, 1)
    rec = _rec(start, direction="SELL", entry=100.0, sl=110.0, tp2=75.0)
    df = _frame(start, [100] * 60, lows=[100, 90, 70] + [100] * 57)
    outcome, r = resolve(rec, df, 40)
    assert outcome == "tp2" and r == pytest.approx(2.5)


def test_nothing_after_the_record_is_pending():
    start = _ms(2026, 1, 1)
    df = _frame(start, [100] * 5)
    assert resolve(_rec(start + 10 * H4), df, 40) == ("pending", None)


def test_an_open_trade_is_not_scored_at_todays_close():
    """The bug this runner fixes: 5 bars in with a 40-bar time stop, the trade is still open."""
    start = _ms(2026, 1, 1)
    df = _frame(start, [100, 101, 102, 103, 104, 105])
    assert resolve(_rec(start), df, 40) == ("pending", None)


def test_a_trade_that_ran_its_full_time_stop_is_scored():
    start = _ms(2026, 1, 1)
    df = _frame(start, [100] * 60)
    outcome, r = resolve(_rec(start), df, 40)
    assert outcome == "time" and r == pytest.approx(0.0)


def test_the_cli_evaluator_also_leaves_open_trades_pending(tmp_path):
    start = _ms(2026, 1, 1)
    path = tmp_path / "signals.jsonl"
    path.write_text(json.dumps(_rec(start, signal="BUY")) + "\n", encoding="utf-8")
    rows = evaluate_journal(str(path), {("BTCUSDT", "4h"): _frame(start, [100, 101, 102])}, 40)
    assert rows[0]["outcome"] == "pending"


def test_the_cli_evaluator_exits_at_tp2_not_tp1(tmp_path):
    start = _ms(2026, 1, 1)
    path = tmp_path / "signals.jsonl"
    path.write_text(json.dumps(_rec(start, signal="BUY")) + "\n", encoding="utf-8")
    df = _frame(start, [100] * 60, highs=[100, 116, 130] + [100] * 57)   # passes TP1 (115) then TP2 (125)
    row = evaluate_journal(str(path), {("BTCUSDT", "4h"): df}, 40)[0]
    assert row["outcome"] == "tp2" and row["r_multiple"] == pytest.approx(2.5)


# ------------------------------------------------------------------ the report
def test_manual_scans_are_counted_but_kept_out_of_calibration():
    start = _ms(2026, 1, 5)
    df = _frame(start, [100] * 60, highs=[100, 130] + [100] * 58)
    recs = [_rec(start, source="scheduled"), _rec(start, source="manual", symbol="BTCUSDT")]
    rep = forward_report(recs, {("BTCUSDT", "4h"): df}, 40)
    assert rep.by_source == {"scheduled": 1, "manual": 1}
    assert rep.independent == 1


def test_buy_and_sell_calls_are_scored_as_trades():
    start = _ms(2026, 1, 5)
    df = _frame(start, [100] * 60, highs=[100, 130] + [100] * 58)
    rep = forward_report([_rec(start, signal="BUY", score=85)], {("BTCUSDT", "4h"): df}, 40)
    assert rep.trades == [pytest.approx(2.5)]


def test_the_digest_refuses_to_judge_a_thin_sample(settings: Settings):
    rep = ForwardReport(total=30, first_ts=_ms(2026, 9, 1), independent=12, resolved=12,
                        base_rate=0.25, auc=(0.71, 0.40, 0.95))
    text = format_digest(rep, settings)
    assert "Too few resolved to judge" in text
    assert "0.71" not in text   # a number this thin must not be shown as a result


def test_the_digest_gives_a_verdict_once_there_is_enough(settings: Settings):
    rep = ForwardReport(total=900, first_ts=_ms(2026, 6, 1), independent=MIN_RESOLVED_TO_JUDGE + 10,
                        resolved=MIN_RESOLVED_TO_JUDGE + 10, base_rate=0.13, auc=(0.52, 0.44, 0.60))
    text = format_digest(rep, settings)
    assert "not distinguishable from a coin flip" in text and "0.520" in text


def test_an_empty_journal_says_so(settings: Settings):
    assert "Nothing recorded yet" in format_digest(ForwardReport(), settings)


# ------------------------------------------------------------------ the journal file
def test_neutral_reports_are_recorded_now(tmp_path, settings: Settings):
    from tests.test_decision_engine import _all_bullish
    from app.asset_classifier import classify
    from app.decision_engine import DecisionEngine, ScanInputs
    from app.schemas import Signal, Timeframe

    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4,
                                                          analyses=_all_bullish()))
    report.signal = Signal.NEUTRAL   # the journal used to drop these; the forward test needs them
    path = tmp_path / "j.jsonl"
    SignalJournal(str(path), source="scheduled").record(report)
    rows = read_journal(path)
    assert len(rows) == 1
    assert rows[0]["signal"] == "NEUTRAL" and rows[0]["source"] == "scheduled"
    assert rows[0]["score"] == report.score and rows[0]["tp2"] == report.levels.tp2


def test_a_torn_last_line_is_skipped(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps({"ts_ms": 1}) + "\n" + '{"ts_ms": 2, "sym', encoding="utf-8")
    assert read_journal(path) == [{"ts_ms": 1}]


def test_a_missing_journal_reads_as_empty(tmp_path):
    assert read_journal(tmp_path / "nope.jsonl") == []


# ------------------------------------------------------------------ the runner
class _Report:
    def __init__(self, signal: str) -> None:
        self.signal = type("S", (), {"value": signal})()


class _Orchestrator:
    def __init__(self, fail: set[str]) -> None:
        self.fail, self.calls = fail, []

    async def scan(self, sym, tf):  # noqa: ANN001, ANN201
        self.calls.append((sym, tf.value))
        if sym in self.fail:
            raise RuntimeError("upstream down")
        return _Report("NEUTRAL")


def test_one_failing_symbol_does_not_stop_the_watchlist(settings: Settings):
    s = settings.model_copy(update={"journal_watchlist": "BTCUSDT,BADUSDT,ETHUSDT"})
    orch = _Orchestrator({"BADUSDT"})
    summary = asyncio.run(JournalRunner(s, orch, market=None, journal_path="x", pause_seconds=0).scan_watchlist())
    assert [c[0] for c in orch.calls] == ["BTCUSDT", "BADUSDT", "ETHUSDT"]
    assert summary.scanned == 2 and summary.failed == ["BADUSDT"]
    assert all(c[1] == "4h" for c in orch.calls)


def test_the_runner_rejects_an_unknown_timeframe(settings: Settings):
    with pytest.raises(ValueError):
        JournalRunner(settings.model_copy(update={"journal_timeframe": "3h"}), None, None, "x")


def test_the_watchlist_is_deduplicated_and_normalised(settings: Settings):
    s = settings.model_copy(update={"journal_watchlist": " btcusdt, ETHUSDT ,BTCUSDT,,"})
    assert s.journal_symbols == ("BTCUSDT", "ETHUSDT")
