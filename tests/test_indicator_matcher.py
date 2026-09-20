from __future__ import annotations

import asyncio

from pydantic import SecretStr

from app.custom_indicators import DEFAULT_RULE, CustomIndicatorRules, IndicatorRule
from app.indicator_matcher import IndicatorMatcher, build_matcher
from app.schemas import AlertSignal, PineAlert

RULES = CustomIndicatorRules({
    "SuperTrend_V2": IndicatorRule(bucket="trend", points=6, max_age_bars=3),
    "SqueezeMomentum": IndicatorRule(bucket="momentum", points=5, value="mom", bullish_above=0, bearish_below=0),
})


class _Answer:
    def __init__(self, choice: str, confidence: float) -> None:
        self.choice = choice
        self.confidence = confidence


class _Response:
    def __init__(self, answers: dict[str, _Answer]) -> None:
        self.choices = answers


class FakeClient:
    def __init__(self, answers: dict[str, tuple[str, float]] | None = None, error: Exception | None = None,
                 raw: object = None) -> None:
        self._answers = answers or {}
        self._error = error
        self._raw = raw
        self.calls: list[dict] = []

    async def system_one(self, state, questions, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"state": state, "questions": questions})
        if self._error is not None:
            raise self._error
        if self._raw is not None:
            return self._raw
        return _Response({n: _Answer(*self._answers[n]) for n in questions if n in self._answers})


class FakeRedis:
    def __init__(self, seed: dict[str, str] | None = None, fail: bool = False) -> None:
        self.store = dict(seed or {})
        self.fail = fail
        self.writes: list[tuple[str, str]] = []

    async def get(self, key: str):  # noqa: ANN201
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):  # noqa: ANN201
        if self.fail:
            raise RuntimeError("redis down")
        self.writes.append((key, value))
        self.store[key] = value


def resolve(matcher: IndicatorMatcher, names, rules: CustomIndicatorRules = RULES):  # noqa: ANN001
    return asyncio.run(matcher.resolve(names, rules))


def _matcher(answers=None, error=None, raw=None, cache=None, min_confidence=0.7):
    client = FakeClient(answers, error, raw)
    return IndicatorMatcher(client, cache=cache, min_confidence=min_confidence), client


# ------------------------------------------------------------------ the bug this fixes
def test_a_drifted_name_silently_scores_on_defaults_today():
    assert not RULES.has("SuperTrend V2")
    assert RULES.rule_for("SuperTrend V2") is DEFAULT_RULE
    assert DEFAULT_RULE.bucket == "indicators"  # not the trend bucket the user configured


def test_exact_names_never_reach_the_model():
    matcher, client = _matcher()

    matched, unmatched = resolve(matcher, ["SuperTrend_V2", "squeezemomentum"])

    assert matched == {} and unmatched == []
    assert client.calls == []


# ------------------------------------------------------------------ matching
def test_a_drifted_name_is_matched_to_its_configured_rule():
    matcher, _ = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.94)})

    matched, unmatched = resolve(matcher, ["SuperTrend V2"])

    assert matched["SuperTrend V2"].bucket == "trend"
    assert matched["SuperTrend V2"].points == 6
    assert unmatched == []


def test_a_genuinely_different_indicator_stays_unmatched():
    matcher, _ = _matcher({"MyOtherOsc": ("none_of_these", 0.9)})

    matched, unmatched = resolve(matcher, ["MyOtherOsc"])

    assert matched == {}
    assert unmatched == ["MyOtherOsc"]


def test_low_confidence_match_is_rejected():
    matcher, _ = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.41)}, min_confidence=0.7)

    matched, unmatched = resolve(matcher, ["SuperTrend V2"])

    assert matched == {}
    assert unmatched == ["SuperTrend V2"]


def test_a_rule_name_that_does_not_exist_is_rejected():
    matcher, _ = _matcher({"SuperTrend V2": ("NotConfigured", 0.99)})

    matched, _ = resolve(matcher, ["SuperTrend V2"])

    assert matched == {}


def test_several_names_are_matched_in_one_request():
    matcher, client = _matcher({
        "SuperTrend V2": ("SuperTrend_V2", 0.9),
        "Squeeze Momentum": ("SqueezeMomentum", 0.9),
    })

    matched, unmatched = resolve(matcher, ["SuperTrend V2", "Squeeze Momentum"])

    assert len(client.calls) == 1
    assert set(client.calls[0]["questions"]) == {"SuperTrend V2", "Squeeze Momentum"}
    assert set(matched) == {"SuperTrend V2", "Squeeze Momentum"}
    assert unmatched == []


def test_duplicate_alert_names_are_asked_once():
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})

    resolve(matcher, ["SuperTrend V2", "SuperTrend V2", "SuperTrend V2"])

    assert list(client.calls[0]["questions"]) == ["SuperTrend V2"]


def test_nothing_is_asked_when_no_rules_are_configured():
    matcher, client = _matcher({"Whatever": ("none_of_these", 0.9)})

    matched, unmatched = resolve(matcher, ["Whatever"], CustomIndicatorRules({}))

    assert matched == {} and unmatched == ["Whatever"]
    assert client.calls == []


def test_upstream_failure_leaves_everything_unmatched():
    matcher, _ = _matcher(error=RuntimeError("boom"))

    matched, unmatched = resolve(matcher, ["SuperTrend V2"])

    assert matched == {} and unmatched == ["SuperTrend V2"]


def test_malformed_response_leaves_everything_unmatched():
    matcher, _ = _matcher(raw=object())

    matched, unmatched = resolve(matcher, ["SuperTrend V2"])

    assert matched == {} and unmatched == ["SuperTrend V2"]


def test_disabled_matcher_passes_names_through():
    matcher = IndicatorMatcher(None)

    matched, unmatched = resolve(matcher, ["SuperTrend V2"])

    assert not matcher.enabled
    assert matched == {} and unmatched == ["SuperTrend V2"]


# ------------------------------------------------------------------ caching
def test_a_name_is_only_ever_asked_once():
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})

    resolve(matcher, ["SuperTrend V2"])
    matched, _ = resolve(matcher, ["SuperTrend V2"])

    assert len(client.calls) == 1
    assert matched["SuperTrend V2"].bucket == "trend"


def test_a_no_match_verdict_is_remembered_too():
    matcher, client = _matcher({"MyOtherOsc": ("none_of_these", 0.9)})

    resolve(matcher, ["MyOtherOsc"])
    resolve(matcher, ["MyOtherOsc"])

    assert len(client.calls) == 1


def test_verdicts_are_written_to_redis():
    cache = FakeRedis()
    matcher, _ = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)}, cache=cache)

    resolve(matcher, ["SuperTrend V2"])

    assert len(cache.writes) == 1
    key, value = cache.writes[0]
    assert key.startswith("indicator_rule:") and key.endswith(":supertrend v2")
    assert value == "SuperTrend_V2"


def test_a_cached_verdict_skips_the_request():
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})
    resolve(matcher, ["SuperTrend V2"])
    cache = FakeRedis(dict(_seed(matcher)))

    fresh, fresh_client = _matcher(cache=cache)
    matched, _ = resolve(fresh, ["SuperTrend V2"])

    assert fresh_client.calls == []
    assert matched["SuperTrend V2"].bucket == "trend"
    assert client.calls  # the first matcher did do the work


def test_changing_the_configured_rules_invalidates_the_cache():
    # A verdict is only valid for the rule set it was chosen from.
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})
    resolve(matcher, ["SuperTrend V2"])

    wider = CustomIndicatorRules({**{n: RULES.named(n) for n in RULES.names},
                                  "SuperTrendV2Pro": IndicatorRule(bucket="trend", points=9)})
    resolve(matcher, ["SuperTrend V2"], wider)

    assert len(client.calls) == 2


def test_a_broken_cache_does_not_break_matching():
    matcher, _ = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)}, cache=FakeRedis(fail=True))

    matched, _ = resolve(matcher, ["SuperTrend V2"])

    assert matched["SuperTrend V2"].bucket == "trend"


def _seed(matcher: IndicatorMatcher) -> dict[str, str]:
    return {f"indicator_rule:{k}": v for k, v in matcher._memo.items()}  # noqa: SLF001


# ------------------------------------------------------------------ question construction
def test_options_are_the_configured_rules_plus_a_no_match_outcome():
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})

    resolve(matcher, ["SuperTrend V2"])

    criteria = client.calls[0]["questions"]["SuperTrend V2"].criteria
    assert set(criteria) == {"SuperTrend_V2", "SqueezeMomentum", "none_of_these"}


def test_the_alert_name_is_in_the_instructions_not_just_the_question_id():
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})

    resolve(matcher, ["SuperTrend V2"])

    assert "SuperTrend V2" in client.calls[0]["questions"]["SuperTrend V2"].instructions


def test_state_describes_what_each_configured_rule_does():
    matcher, client = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})

    resolve(matcher, ["SuperTrend V2"])

    configured = client.calls[0]["state"]["configured_rules"]
    by_name = {r["name"]: r for r in configured}
    assert by_name["SuperTrend_V2"]["bucket"] == "trend"
    assert by_name["SqueezeMomentum"]["reads_value"] == "mom"


# ------------------------------------------------------------------ end to end
def test_a_matched_rule_scores_through_the_engine_path():
    """extra_rules is keyed by the alert's own name, which is how rule_for() finds it."""
    matcher, _ = _matcher({"SuperTrend V2": ("SuperTrend_V2", 0.9)})
    matched, _ = resolve(matcher, ["SuperTrend V2"])

    now = 1_700_000_000_000
    alert = PineAlert(ticker="BTCUSDT", timeframe="4h", indicator="SuperTrend V2", signal=AlertSignal.BUY,
                      price=1.0, values={}, alert_ts_ms=now, received_at_ms=now)

    before = RULES.contributions([alert], now)
    after = RULES.merged(matched).contributions([alert], now)

    assert before[0].bucket == "indicators" and before[0].bullish == DEFAULT_RULE.points
    assert after[0].bucket == "trend" and after[0].bullish == 6


# ------------------------------------------------------------------ wiring
def test_no_matcher_without_a_key(settings):
    assert build_matcher(settings) is None


def test_no_matcher_when_matching_is_off(settings):
    off = settings.model_copy(update={"typesafe_api_key": SecretStr("sk-test"),
                                      "typesafe_indicator_matching": False})
    assert build_matcher(off) is None


def test_matcher_built_when_enabled(settings):
    on = settings.model_copy(update={"typesafe_api_key": SecretStr("sk-test")})
    built = build_matcher(on)
    assert built is not None and built.enabled

    from app.clients.typesafe import aclose

    asyncio.run(aclose(built._client))  # noqa: SLF001 - test cleanup
