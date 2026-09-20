from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, get_args

from app.clients.tradingview_ws import ScriptInfo, StudyResult
from app.custom_indicators import Bucket

logger = logging.getLogger(__name__)

# A study pulled from the TradingView account feed always arrives with signal=NEUTRAL (see
# ScanOrchestrator._fetch_studies), so it only contributes through `IndicatorRule.value` — the plot key and its
# bull/bear thresholds. Picking those by hand is the one piece of manual tuning /indicators add cannot avoid;
# this module asks TypeSafe (Jev) to read the script's own metadata and choose them.

BUCKET_CRITERIA: dict[str, str] = {
    "trend": (
        "Trend and structure. The script's main job is identifying trend direction, trend strength or market "
        "structure: moving averages and ribbons, SuperTrend, ADX/DMI, break-of-structure or swing labelling."
    ),
    "momentum": (
        "Momentum and volatility. An oscillator or volatility gauge: RSI, MACD, stochastics, Bollinger width or "
        "squeeze, volume oscillators, rate of change, divergence detectors."
    ),
    "indicators": (
        "General technical rating. A composite or discretionary buy/sell script that does not clearly belong to "
        "trend or momentum: multi-factor scoring systems, signal generators, strategy entries."
    ),
    "context": (
        "Market context outside the instrument's own price action: on-chain flows, relative strength versus another "
        "instrument or sector, volume profile and value area, seasonality, sentiment or positioning data."
    ),
}

SHAPE_CRITERIA: dict[str, str] = {
    "zero_centred": (
        "Oscillates around zero. A positive reading means bullish and a negative reading means bearish, with the "
        "sign carrying the direction: MACD histogram, momentum, rate of change, CCI."
    ),
    "bounded_0_100": (
        "Bounded roughly between 0 and 100 with a midline near 50. Readings above the midline lean bullish and "
        "below it lean bearish: RSI, stochastic, Money Flow Index, Williams %R rescaled to 0-100."
    ),
    "binary_state": (
        "Emits a discrete state rather than a magnitude: 1 and -1, 1 and 0, or a true/false flag marking a regime, "
        "a direction, or a condition that has triggered."
    ),
    "price_overlay": (
        "Plots a value in the instrument's own price units, drawn over the candles: a moving average, SuperTrend "
        "line, VWAP, band edge or support/resistance level. The number only means something compared with the "
        "current price, never against a fixed threshold."
    ),
}

WEIGHT_LEVELS: list[str] = [
    "Cosmetic or informational only: a label, a counter, or a plot that restates something already visible on the chart.",
    "Weak supporting evidence: a common single-factor reading that largely repeats what a standard indicator already says.",
    "Ordinary confirmation: a well-known indicator that adds genuine but replaceable evidence to a setup.",
    "Strong evidence: a multi-factor or filtered signal that only fires on a specific and fairly rare condition.",
    "Primary decision signal: a complete entry system the script's own author intends to be traded on its own.",
]

# Points awarded at the lowest and highest weight level; the Score lands anywhere between.
_POINTS_MIN, _POINTS_MAX = 2.0, 12.0
_RECENT_VALUES = 6

# Keeps the options offered to the model in step with the buckets IndicatorRule actually accepts.
_BUCKETS: tuple[str, ...] = get_args(Bucket)
assert set(BUCKET_CRITERIA) == set(_BUCKETS), "bucket criteria drifted from IndicatorRule buckets"


class _SystemOneClient(Protocol):
    async def system_one(self, state: Any, questions: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Advice:
    """What the advisor concluded. `rule_opts` is None when /indicators add should keep its manual defaults."""

    reason: str
    rule_opts: dict[str, Any] | None = None
    confidence: float = 0.0
    details: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.rule_opts is not None


@dataclass
class _PlotEvidence:
    key: str
    values: list[float] = field(default_factory=list)

    @property
    def observed(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key}
        if self.values:
            out["recent_values"] = [round(v, 6) for v in self.values[-_RECENT_VALUES:]]
            out["min_observed"] = round(min(self.values), 6)
            out["max_observed"] = round(max(self.values), 6)
            out["distinct_values"] = len({round(v, 6) for v in self.values})
        return out


def _plot_titles(meta: dict[str, Any] | None) -> list[str]:
    """Plot keys as the websocket client names them, derived from pine-facade metadata alone (no chart session)."""
    info = (meta or {}).get("metaInfo") or {}
    styles = info.get("styles") or {}
    titles: list[str] = []
    for plot in info.get("plots") or []:
        pid = plot.get("id", f"plot_{len(titles)}")
        title = (styles.get(pid) or {}).get("title") or pid
        titles.append(re.sub(r"[^A-Za-z0-9_]+", "_", str(title)).strip("_") or pid)
    return titles


def _description(meta: dict[str, Any] | None) -> str:
    info = (meta or {}).get("metaInfo") or {}
    for key in ("description", "shortDescription", "short_description"):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:500]
    return ""


def _inputs(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    info = (meta or {}).get("metaInfo") or {}
    out: list[dict[str, Any]] = []
    for inp in info.get("inputs") or []:
        name = inp.get("name") or inp.get("id")
        if not name or name in ("text", "pineId", "pineVersion"):
            continue
        out.append({"name": str(name), "default": inp.get("defval")})
    return out[:12]


def _evidence(plots: list[str], study: StudyResult | None) -> list[_PlotEvidence]:
    ev = [_PlotEvidence(key=k) for k in plots]
    if study is None:
        return ev
    by_key = {e.key: e for e in ev}
    for row in study.rows:
        for key, val in row.items():
            if key == "ts":
                continue
            target = by_key.get(key)
            if target is not None and isinstance(val, (int, float)):
                target.values.append(float(val))
    return ev


def _thresholds(shape: str, values: list[float]) -> tuple[float, float] | None:
    """Model names the shape; code derives the numbers from what the plot actually printed."""
    if shape == "price_overlay":
        return None
    if shape == "bounded_0_100":
        if values and (min(values) < -5.0 or max(values) > 105.0):
            return None  # observed range contradicts the judgment — do not guess
        return 55.0, 45.0
    if shape == "binary_state":
        # 1/0 flags need a midpoint; 1/-1 flags work off zero.
        return (0.5, 0.5) if values and min(values) >= 0.0 else (0.0, 0.0)
    return 0.0, 0.0  # zero_centred


def _points(score: float, levels: int) -> float:
    span = max(levels - 1, 1)
    raw = _POINTS_MIN + (max(0.0, min(float(score), span)) / span) * (_POINTS_MAX - _POINTS_MIN)
    return round(raw * 2) / 2


class StudyAdvisor:
    """Reads a TradingView script's own metadata and asks TypeSafe how it should score in the confluence matrix."""

    def __init__(self, client: _SystemOneClient | None, min_confidence: float = 0.55, model: str | None = None) -> None:
        self._client = client
        self._min_confidence = min_confidence
        self._model = model

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        closer = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                logger.warning("typesafe client close failed", extra={"error": str(exc)})

    async def suggest(
        self,
        script: ScriptInfo,
        meta: dict[str, Any] | None = None,
        study: StudyResult | None = None,
    ) -> Advice:
        if self._client is None:
            return Advice(reason="TypeSafe auto-configuration is off (no TYPESAFE_API_KEY).")

        plots = list(study.plots) if study is not None and study.plots else _plot_titles(meta)
        plots = [p for p in dict.fromkeys(plots) if p]
        if not plots:
            return Advice(reason="Could not read the script's plot list — configure plot= manually.")

        evidence = _evidence(plots, study)
        state = {
            "indicator": {"name": script.name, "kind": script.kind, "source": script.source},
            "description": _description(meta),
            "inputs": _inputs(meta),
            "plots": [e.observed for e in evidence],
        }

        try:
            from typesafe_sdk import Choice, Score
        except ImportError:  # pragma: no cover - guarded at construction
            return Advice(reason="typesafe-sdk is not installed — configure plot= above= below= manually.")

        questions: dict[str, Any] = {
            "bucket": Choice(
                instructions=(
                    "A trading scanner scores a setup across several evidence buckets. Which bucket should this "
                    "TradingView indicator contribute to? Judge it from `indicator.name`, `description` and the "
                    "names in `plots`."
                ),
                criteria=dict(BUCKET_CRITERIA),
            ),
            "shape": Choice(
                instructions=(
                    "The scanner reads one numeric plot from this indicator and compares it against fixed bullish "
                    "and bearish thresholds. What kind of number does this indicator's main plot produce? Use "
                    "`indicator.name`, `description` and the `recent_values`, `min_observed` and `max_observed` "
                    "shown for each entry in `plots`."
                ),
                criteria=dict(SHAPE_CRITERIA),
            ),
            "weight": Score(
                instructions=(
                    "How much weight does this indicator deserve in a multi-factor trade decision, relative to "
                    "standard indicators like a moving-average ribbon or RSI? Judge the strength of the evidence "
                    "the script produces, not how popular it is."
                ),
                criteria=list(WEIGHT_LEVELS),
            ),
        }
        if len(plots) > 1:
            questions["signal_plot"] = Choice(
                instructions=(
                    "This indicator publishes several plots. Which one carries the directional signal a trader "
                    "would act on — the line whose value decides bullish versus bearish? Prefer the main output "
                    "over bands, fills, thresholds, colours or diagnostic series."
                ),
                criteria={e.key: self._plot_criterion(e) for e in evidence},
            )

        try:
            kwargs: dict[str, Any] = {"model": self._model} if self._model else {}
            response = await self._client.system_one(state=state, questions=questions, **kwargs)
        except Exception as exc:  # noqa: BLE001 - never block /indicators add on an upstream failure
            logger.warning("typesafe study advice failed", extra={"script": script.name, "error": str(exc)})
            return Advice(reason=f"TypeSafe unavailable ({type(exc).__name__}) — kept manual defaults.")

        return self._interpret(response, evidence, plots)

    @staticmethod
    def _plot_criterion(ev: _PlotEvidence) -> dict[str, Any]:
        out: dict[str, Any] = {"what": f"The plot named {ev.key!r}."}
        if ev.values:
            recent = ", ".join(f"{v:g}" for v in ev.values[-_RECENT_VALUES:])
            out["observed"] = f"Recent values: {recent}. Range {min(ev.values):g} to {max(ev.values):g}."
        return out

    def _interpret(self, response: Any, evidence: list[_PlotEvidence], plots: list[str]) -> Advice:
        try:
            choices, scores = response.choices, response.scores
            bucket_ans, shape_ans = choices["bucket"], choices["shape"]
            weight_ans = scores["weight"]
        except (AttributeError, KeyError, TypeError) as exc:
            logger.warning("typesafe response not understood", extra={"error": str(exc)})
            return Advice(reason="TypeSafe returned an unexpected response — kept manual defaults.")

        bucket = str(bucket_ans.choice)
        shape = str(shape_ans.choice)
        if bucket not in _BUCKETS or shape not in SHAPE_CRITERIA:
            return Advice(reason="TypeSafe returned an unknown option — kept manual defaults.")

        plot_ans = response.choices.get("signal_plot") if len(plots) > 1 else None
        plot = str(plot_ans.choice) if plot_ans is not None else plots[0]
        if plot not in plots:
            return Advice(reason="TypeSafe picked a plot that does not exist — kept manual defaults.")

        # Weakest link across the answers code actually consumes; the weight Score is advisory (a spread there only
        # means "somewhere in the middle", which is harmless), so it does not gate.
        consumed = [float(bucket_ans.confidence), float(shape_ans.confidence)]
        if plot_ans is not None:
            consumed.append(float(plot_ans.confidence))
        confidence = min(consumed)

        if confidence < self._min_confidence:
            return Advice(
                reason=(
                    f"TypeSafe was unsure ({confidence:.0%} < {self._min_confidence:.0%}) — kept manual defaults. "
                    f"Its best guess was {bucket}/{shape} on {plot}."
                ),
                confidence=confidence,
            )

        values = next((e.values for e in evidence if e.key == plot), [])
        bounds = _thresholds(shape, values)
        if bounds is None:
            hint = (
                "it plots in price units, so a fixed threshold means nothing"
                if shape == "price_overlay"
                else "the values it printed contradict that shape"
            )
            return Advice(
                reason=f"TypeSafe read this as {shape} — {hint}. Set plot=/above=/below= manually.",
                confidence=confidence,
            )

        above, below = bounds
        points = _points(float(weight_ans.score), len(WEIGHT_LEVELS))
        level = self._weight_label(weight_ans)
        details = (
            f"bucket {bucket} ({bucket_ans.confidence:.0%})",
            f"{shape} on {plot} ({shape_ans.confidence:.0%}) → bullish > {above:g}, bearish < {below:g}",
            f"{points:g} pts — {level}",
        )
        return Advice(
            reason=f"TypeSafe configured this automatically ({confidence:.0%} confidence).",
            rule_opts={"bucket": bucket, "plot": plot, "above": above, "below": below, "points": points},
            confidence=confidence,
            details=details,
        )

    @staticmethod
    def _weight_label(weight_ans: Any) -> str:
        legend = getattr(weight_ans, "legend", None)
        nearest = int(round(float(weight_ans.score)))
        if isinstance(legend, dict):
            label = legend.get(nearest) or legend.get(str(nearest))
            if isinstance(label, str):
                return label.split(":")[0].strip().lower()
        idx = max(0, min(nearest, len(WEIGHT_LEVELS) - 1))
        return WEIGHT_LEVELS[idx].split(":")[0].strip().lower()


def build_advisor(settings: Any) -> StudyAdvisor | None:
    """None when auto-configuration is off; a disabled advisor is never constructed."""
    if not settings.typesafe_active:
        return None
    try:
        from typesafe_sdk import AsyncTypeSafeClient
    except ImportError:
        logger.warning("TYPESAFE_API_KEY is set but typesafe-sdk is not installed; auto-configuration disabled")
        return None
    client = AsyncTypeSafeClient(
        api_key=settings.typesafe_api_key.get_secret_value(),
        model=settings.typesafe_model,
        timeout=settings.typesafe_timeout_seconds,
    )
    return StudyAdvisor(client, min_confidence=settings.typesafe_min_confidence, model=settings.typesafe_model)


__all__ = ["Advice", "StudyAdvisor", "build_advisor", "BUCKET_CRITERIA", "SHAPE_CRITERIA", "WEIGHT_LEVELS"]
