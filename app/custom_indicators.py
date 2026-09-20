from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.schemas import AlertSignal, PineAlert
from app.timeframes import BAR_MS, parse_timeframe

logger = logging.getLogger(__name__)

Bucket = Literal["trend", "momentum", "indicators", "context"]


class IndicatorRule(BaseModel):
    """How one custom TradingView indicator contributes to the confluence matrix."""

    bucket: Bucket = "indicators"
    points: float = Field(default=5.0, ge=0, le=30)
    max_age_bars: int = Field(default=2, ge=1, le=50)
    value: str | None = Field(default=None, description="Key in alert.values to evaluate instead of the BUY/SELL signal")
    bullish_above: float | None = None
    bearish_below: float | None = None
    opposite_penalty: float = Field(default=0.5, ge=0, le=1)


class Contribution(BaseModel):
    bucket: Bucket
    bullish: float
    bearish: float
    note: str


DEFAULT_RULE = IndicatorRule()


class CustomIndicatorRules:
    def __init__(self, rules: dict[str, IndicatorRule]) -> None:
        self._rules = {k.lower(): v for k, v in rules.items()}
        self._names = sorted(rules)

    @classmethod
    def load(cls, path: str) -> CustomIndicatorRules:
        p = Path(path)
        if not p.exists():
            logger.info("no custom indicator rules file; using defaults", extra={"path": path})
            return cls({})
        rules: dict[str, IndicatorRule] = {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("custom indicator rules unreadable", extra={"path": path, "error": str(exc)})
            return cls({})
        for name, spec in raw.items():
            if name.startswith("_") or not isinstance(spec, dict):
                continue
            try:
                rules[name] = IndicatorRule.model_validate(spec)
            except ValidationError as exc:
                logger.error("invalid custom indicator rule", extra={"indicator": name, "error": str(exc)})
        return cls(rules)

    def rule_for(self, indicator: str) -> IndicatorRule:
        return self._rules.get(indicator.lower(), DEFAULT_RULE)

    def has(self, indicator: str) -> bool:
        """Whether `rule_for` would find a real rule rather than silently returning DEFAULT_RULE."""
        return indicator.lower() in self._rules

    def named(self, indicator: str) -> IndicatorRule | None:
        return self._rules.get(indicator.lower())

    def merged(self, extra: dict[str, IndicatorRule]) -> CustomIndicatorRules:
        if not extra:
            return self
        combined = {**{n: self._rules[n.lower()] for n in self._names}, **extra}
        return CustomIndicatorRules(combined)

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def contributions(self, alerts: list[PineAlert], now_ms: int) -> list[Contribution]:
        latest: dict[tuple[str, str], PineAlert] = {}
        for alert in sorted(alerts, key=lambda a: a.received_at_ms, reverse=True):
            latest.setdefault((alert.indicator.lower(), alert.timeframe), alert)

        out: list[Contribution] = []
        for alert in latest.values():
            rule = self.rule_for(alert.indicator)
            tf = parse_timeframe(alert.timeframe)
            bar_ms = BAR_MS[tf] if tf else 3_600_000
            if now_ms - alert.received_at_ms > rule.max_age_bars * bar_ms:
                continue

            direction: Literal["bull", "bear"] | None = None
            label = f"{alert.indicator}@{alert.timeframe}"
            if rule.value is not None:
                reading = alert.values.get(rule.value)
                if reading is None:
                    continue
                if rule.bullish_above is not None and reading > rule.bullish_above:
                    direction = "bull"
                elif rule.bearish_below is not None and reading < rule.bearish_below:
                    direction = "bear"
                label += f" {rule.value}={reading:g}"
            elif alert.signal is AlertSignal.BUY:
                direction = "bull"
            elif alert.signal is AlertSignal.SELL:
                direction = "bear"
            if direction is None:
                continue

            penalty = rule.points * rule.opposite_penalty
            if direction == "bull":
                out.append(Contribution(bucket=rule.bucket, bullish=rule.points, bearish=-penalty, note=f"▲ Pine {label} (+{rule.points:g})"))
            else:
                out.append(Contribution(bucket=rule.bucket, bullish=-penalty, bearish=rule.points, note=f"▼ Pine {label} (+{rule.points:g})"))
        return out
