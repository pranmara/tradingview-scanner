from __future__ import annotations

import json
from pathlib import Path

from app.custom_indicators import CustomIndicatorRules, IndicatorRule
from app.schemas import AlertSignal, PineAlert

NOW = 1_800_000_000_000
H4 = 4 * 3_600_000


def _alert(indicator: str, signal: AlertSignal, age_ms: int = 0, values: dict[str, float] | None = None, tf: str = "4h") -> PineAlert:
    return PineAlert(ticker="BTCUSDT", timeframe=tf, indicator=indicator, signal=signal, price=100.0,
                     values=values or {}, received_at_ms=NOW - age_ms)


def test_default_rule_for_unknown_indicator() -> None:
    rules = CustomIndicatorRules({})
    out = rules.contributions([_alert("Whatever", AlertSignal.BUY)], NOW)
    assert len(out) == 1 and out[0].bucket == "indicators" and out[0].bullish == 5.0 and out[0].bearish == -2.5


def test_stale_alert_ignored() -> None:
    rules = CustomIndicatorRules({})
    assert rules.contributions([_alert("X", AlertSignal.BUY, age_ms=3 * H4)], NOW) == []


def test_value_threshold_rule() -> None:
    rules = CustomIndicatorRules({"Osc": IndicatorRule(bucket="trend", points=6, value="osc", bullish_above=10, bearish_below=-10)})
    bull = rules.contributions([_alert("osc", AlertSignal.NEUTRAL, values={"osc": 12.5})], NOW)
    bear = rules.contributions([_alert("Osc", AlertSignal.NEUTRAL, values={"osc": -20})], NOW)
    flat = rules.contributions([_alert("Osc", AlertSignal.NEUTRAL, values={"osc": 0})], NOW)
    assert bull[0].bucket == "trend" and bull[0].bullish == 6
    assert bear[0].bearish == 6 and bear[0].bullish == -3
    assert flat == []


def test_latest_alert_per_indicator_wins() -> None:
    rules = CustomIndicatorRules({})
    alerts = [_alert("ST", AlertSignal.SELL, age_ms=60_000), _alert("ST", AlertSignal.BUY, age_ms=10_000)]
    out = rules.contributions(alerts, NOW)
    assert len(out) == 1 and out[0].bullish > 0


def test_load_rules_file(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"_comment": "x", "Good": {"bucket": "context", "points": 4}, "Bad": {"points": 999}}), encoding="utf-8")
    rules = CustomIndicatorRules.load(str(p))
    assert rules.names == ["Good"]
    assert rules.rule_for("GOOD").bucket == "context"
    assert CustomIndicatorRules.load(str(tmp_path / "missing.json")).names == []
