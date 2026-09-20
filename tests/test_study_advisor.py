from __future__ import annotations

import asyncio

import pytest

from app.clients.tradingview_ws import ScriptInfo, StudyResult
from app.custom_indicators import CustomIndicatorRules
from app.schemas import AlertSignal, PineAlert
from app.study_advisor import (
    BUCKET_CRITERIA,
    SHAPE_CRITERIA,
    WEIGHT_LEVELS,
    StudyAdvisor,
    _points,
    _thresholds,
    build_advisor,
)
from app.study_registry import StudyRegistry

SCRIPT = ScriptInfo(pine_id="USER;abc123", name="Squeeze Momentum Pro", version="last", kind="study", source="user")


class _Answer:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Response:
    """Mirrors SystemOneResponse's .choices / .scores accessors."""

    def __init__(self, choices: dict[str, _Answer], scores: dict[str, _Answer]) -> None:
        self.choices = choices
        self.scores = scores


class FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def system_one(self, state, questions, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"state": state, "questions": questions, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        return self._response


def _response(bucket="momentum", bucket_c=0.9, shape="zero_centred", shape_c=0.9,
              plot=None, plot_c=0.9, score=2.0):
    choices = {
        "bucket": _Answer(choice=bucket, confidence=bucket_c, probabilities={}),
        "shape": _Answer(choice=shape, confidence=shape_c, probabilities={}),
    }
    if plot is not None:
        choices["signal_plot"] = _Answer(choice=plot, confidence=plot_c, probabilities={})
    legend = {i: text for i, text in enumerate(WEIGHT_LEVELS)}
    return _Response(choices, {"weight": _Answer(score=score, confidence=0.7, legend=legend)})


def suggest(advisor: StudyAdvisor, *args, **kwargs):
    """The repo's pytest setup is plain and synchronous; run the coroutine rather than add a plugin."""
    return asyncio.run(advisor.suggest(*args, **kwargs))


def _study(plots: list[str], rows: list[dict[str, float]]) -> StudyResult:
    return StudyResult(pine_id=SCRIPT.pine_id, plots=plots, rows=rows)


META = {
    "metaInfo": {
        "description": "Squeeze momentum histogram with a volatility filter",
        "inputs": [{"name": "Length", "defval": 20}, {"id": "text"}],
        "plots": [{"id": "plot_0"}, {"id": "plot_1"}],
        "styles": {"plot_0": {"title": "Momentum"}, "plot_1": {"title": "Squeeze Flag"}},
    }
}


# ------------------------------------------------------------------ pure helpers
def test_zero_centred_thresholds_straddle_zero():
    assert _thresholds("zero_centred", [-3.0, 1.2, 4.5]) == (0.0, 0.0)


def test_bounded_oscillator_uses_midline_band():
    assert _thresholds("bounded_0_100", [31.0, 55.0, 72.0]) == (55.0, 45.0)


def test_bounded_claim_is_rejected_when_observed_values_contradict_it():
    # Model says RSI-like, plot actually printed price-sized numbers: refuse rather than guess.
    assert _thresholds("bounded_0_100", [64000.0, 65120.5]) is None


def test_binary_flag_without_negatives_gets_a_midpoint():
    assert _thresholds("binary_state", [0.0, 1.0, 1.0, 0.0]) == (0.5, 0.5)


def test_binary_flag_with_negatives_works_off_zero():
    assert _thresholds("binary_state", [1.0, -1.0, 1.0]) == (0.0, 0.0)


def test_price_overlay_is_never_auto_configured():
    assert _thresholds("price_overlay", [64000.0]) is None


def test_points_span_the_configured_range():
    assert _points(0.0, len(WEIGHT_LEVELS)) == 2.0
    assert _points(4.0, len(WEIGHT_LEVELS)) == 12.0
    assert 2.0 < _points(2.0, len(WEIGHT_LEVELS)) < 12.0


def test_points_clamp_out_of_range_scores():
    assert _points(-1.0, len(WEIGHT_LEVELS)) == 2.0
    assert _points(99.0, len(WEIGHT_LEVELS)) == 12.0


# ------------------------------------------------------------------ suggest()
def test_confident_answer_produces_rule_options():
    client = FakeClient(_response(bucket="momentum", shape="zero_centred", plot="Momentum", score=3.0))
    advisor = StudyAdvisor(client, min_confidence=0.55)
    study = _study(["Momentum", "Squeeze_Flag"], [{"ts": 1.0, "Momentum": -2.0, "Squeeze_Flag": 1.0},
                                                  {"ts": 2.0, "Momentum": 3.5, "Squeeze_Flag": 0.0}])

    advice = suggest(advisor, SCRIPT, meta=META, study=study)

    assert advice.applied
    assert advice.rule_opts == {"bucket": "momentum", "plot": "Momentum", "above": 0.0, "below": 0.0,
                                "points": _points(3.0, len(WEIGHT_LEVELS))}
    assert advice.confidence == pytest.approx(0.9)


def test_plot_question_is_skipped_for_single_plot_scripts():
    client = FakeClient(_response(plot=None))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, study=_study(["Momentum"], [{"ts": 1.0, "Momentum": 1.0}]))

    assert "signal_plot" not in client.calls[0]["questions"]
    assert advice.applied and advice.rule_opts is not None
    assert advice.rule_opts["plot"] == "Momentum"


def test_plot_question_offers_every_plot_as_an_option():
    client = FakeClient(_response(plot="Momentum"))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    suggest(advisor, SCRIPT, meta=META, study=_study(["Momentum", "Squeeze_Flag"], []))

    assert set(client.calls[0]["questions"]["signal_plot"].criteria) == {"Momentum", "Squeeze_Flag"}


def test_low_confidence_keeps_manual_defaults():
    client = FakeClient(_response(shape_c=0.31, plot="Momentum"))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, meta=META, study=_study(["Momentum", "Squeeze_Flag"], []))

    assert not advice.applied
    assert "unsure" in advice.reason


def test_confidence_is_the_weakest_consumed_answer_not_the_score():
    # The weight Score is deliberately advisory: a spread there must not block auto-configuration.
    client = FakeClient(_response(bucket_c=0.88, shape_c=0.61, plot="Momentum", plot_c=0.77))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, meta=META, study=_study(["Momentum", "Squeeze_Flag"], []))

    assert advice.applied
    assert advice.confidence == pytest.approx(0.61)


def test_price_overlay_falls_back_with_an_explanation():
    client = FakeClient(_response(bucket="trend", shape="price_overlay", plot=None))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, study=_study(["SuperTrend"], [{"ts": 1.0, "SuperTrend": 64000.0}]))

    assert not advice.applied
    assert "price units" in advice.reason


def test_hallucinated_plot_name_is_refused():
    client = FakeClient(_response(plot="NotAPlot"))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, meta=META, study=_study(["Momentum", "Squeeze_Flag"], []))

    assert not advice.applied
    assert "does not exist" in advice.reason


def test_unknown_bucket_is_refused():
    client = FakeClient(_response(bucket="sentiment", plot=None))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, study=_study(["Momentum"], []))

    assert not advice.applied
    assert "unknown option" in advice.reason


def test_upstream_failure_never_blocks_the_add():
    advisor = StudyAdvisor(FakeClient(error=RuntimeError("boom")), min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, study=_study(["Momentum"], []))

    assert not advice.applied
    assert "unavailable" in advice.reason


def test_malformed_response_never_blocks_the_add():
    advisor = StudyAdvisor(FakeClient(object()), min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, study=_study(["Momentum"], []))

    assert not advice.applied
    assert "unexpected response" in advice.reason


def test_plot_names_are_recovered_from_metadata_when_the_probe_fails():
    client = FakeClient(_response(plot="Momentum"))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, meta=META, study=None)

    assert set(client.calls[0]["questions"]["signal_plot"].criteria) == {"Momentum", "Squeeze_Flag"}
    assert advice.applied


def test_no_plots_anywhere_falls_back():
    advisor = StudyAdvisor(FakeClient(_response()), min_confidence=0.55)

    advice = suggest(advisor, SCRIPT, meta={"metaInfo": {}}, study=None)

    assert not advice.applied
    assert "plot list" in advice.reason


def test_state_carries_observed_values_for_the_shape_judgment():
    client = FakeClient(_response(plot="Momentum"))
    advisor = StudyAdvisor(client, min_confidence=0.55)

    suggest(advisor, SCRIPT, meta=META, study=_study(
        ["Momentum", "Squeeze_Flag"],
        [{"ts": 1.0, "Momentum": -2.0, "Squeeze_Flag": 1.0}, {"ts": 2.0, "Momentum": 3.5, "Squeeze_Flag": 0.0}],
    ))

    state = client.calls[0]["state"]
    momentum = next(p for p in state["plots"] if p["key"] == "Momentum")
    assert momentum["min_observed"] == -2.0
    assert momentum["max_observed"] == 3.5
    assert state["indicator"]["name"] == SCRIPT.name
    assert state["description"].startswith("Squeeze momentum")


def test_disabled_advisor_reports_instead_of_calling_out():
    advisor = StudyAdvisor(None)

    advice = suggest(advisor, SCRIPT, study=_study(["Momentum"], []))

    assert not advisor.enabled
    assert not advice.applied


# ------------------------------------------------------------------ wiring
def test_criteria_cover_every_bucket_and_are_non_empty():
    assert set(BUCKET_CRITERIA) == {"trend", "momentum", "indicators", "context"}
    assert all(v.strip() for v in BUCKET_CRITERIA.values())
    assert all(v.strip() for v in SHAPE_CRITERIA.values())
    assert 2 <= len(WEIGHT_LEVELS) <= 10


def test_no_advisor_without_a_key(settings):
    assert settings.typesafe_api_key is None
    assert not settings.typesafe_active
    assert build_advisor(settings) is None


def test_no_advisor_when_autoconfig_is_off(settings):
    off = settings.model_copy(update={"typesafe_api_key": "sk-test", "typesafe_autoconfig": False})
    assert not off.typesafe_active
    assert build_advisor(off) is None


# ------------------------------------------------------------------ end to end
def test_advice_flows_through_to_a_scored_contribution(tmp_path):
    """The whole point: what the advisor returns must survive StudyRegistry and reach the confluence matrix."""
    client = FakeClient(_response(bucket="momentum", shape="bounded_0_100", plot="Momentum", score=3.0))
    advisor = StudyAdvisor(client, min_confidence=0.55)
    advice = suggest(advisor, SCRIPT, meta=META,
                     study=_study(["Momentum", "Squeeze_Flag"], [{"ts": 1.0, "Momentum": 62.0, "Squeeze_Flag": 1.0}]))
    assert advice.rule_opts is not None

    registry = StudyRegistry(str(tmp_path / "active.json"))
    name, rule = registry.add(SCRIPT, advice.rule_opts, {})
    assert rule.bucket == "momentum"
    assert rule.value == "Momentum"
    assert (rule.bullish_above, rule.bearish_below) == (55.0, 45.0)

    now = 1_700_000_000_000
    alert = PineAlert(ticker="BTCUSDT", timeframe="4h", indicator=name, signal=AlertSignal.NEUTRAL,
                      price=0.0, values={"Momentum": 62.0}, alert_ts_ms=now, received_at_ms=now)
    contributions = CustomIndicatorRules({}).merged({name: rule}).contributions([alert], now)

    assert len(contributions) == 1
    assert contributions[0].bucket == "momentum"
    assert contributions[0].bullish == rule.points
    assert contributions[0].bearish < 0  # opposite side is penalised, as with any Pine contribution
