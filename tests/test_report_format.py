from __future__ import annotations

import pytest

from app.asset_classifier import classify
from app.config import Settings
from app.decision_engine import DecisionEngine, ScanInputs
from app.schemas import Signal, Timeframe
from app.telegram_bot import HIGH_COST_R, format_report
from tests.test_decision_engine import _all_bullish


def _report(settings: Settings, **overrides):  # noqa: ANN003, ANN202
    s = settings.model_copy(update=overrides) if overrides else settings
    return DecisionEngine(s).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish()))


# ------------------------------------------------------------------ 4: round-trip cost in R
def test_cost_is_the_backtesters_exact_charge(settings: Settings) -> None:
    lv = _report(settings).levels
    assert lv is not None
    cost_frac = (2 * settings.backtest_fee_bps + settings.backtest_slippage_bps) / 10_000
    assert lv.round_trip_cost_r == pytest.approx(cost_frac * lv.entry / lv.risk_per_unit)


def test_a_tighter_stop_costs_more_r(settings: Settings) -> None:
    """The finding that made 1h unviable: the same bps is a larger share of a smaller risk."""
    def levels(mult: float):  # noqa: ANN202
        analyses = _all_bullish()
        for ta in analyses.values():
            if ta.institutional is not None:
                ta.institutional.sweep_bullish_level = None   # else the stop sits off the sweep, not ATR
        s = settings.model_copy(update={"atr_multiplier": mult})
        return DecisionEngine(s).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4,
                                                     analyses=analyses)).levels

    wide, tight = levels(3.0), levels(0.5)
    assert wide is not None and tight is not None
    assert tight.round_trip_cost_r > wide.round_trip_cost_r


def test_the_report_prints_the_cost(settings: Settings) -> None:
    text = format_report(_report(settings))
    assert "round trip (fees + slippage)" in text


def test_a_high_cost_is_flagged(settings: Settings) -> None:
    r = _report(settings)
    assert r.levels is not None
    r.levels.round_trip_cost_r = HIGH_COST_R + 0.05
    assert "high at this stop distance" in format_report(r)


def test_a_modest_cost_is_not_flagged(settings: Settings) -> None:
    r = _report(settings)
    assert r.levels is not None
    r.levels.round_trip_cost_r = HIGH_COST_R - 0.05
    assert "high at this stop distance" not in format_report(r)


# ------------------------------------------------------------------ 5: the report says what the evidence is
@pytest.mark.parametrize("signal", [Signal.BUY, Signal.SELL, Signal.WATCH])
def test_actionable_reports_carry_the_caveat(settings: Settings, signal: Signal) -> None:
    r = _report(settings)
    r.signal = signal
    text = format_report(r)
    assert "Score not validated" in text and "/journal" in text


def test_a_neutral_report_does_not_nag(settings: Settings) -> None:
    r = _report(settings)
    r.signal = Signal.NEUTRAL
    assert "Score not validated" not in format_report(r)


def test_the_caveat_sits_directly_under_the_score(settings: Settings) -> None:
    r = _report(settings)
    r.signal = Signal.BUY
    lines = format_report(r).splitlines()
    assert lines[1].startswith("Score") and "Score not validated" in lines[2]
