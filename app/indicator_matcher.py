from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any, Protocol

from redis.asyncio import Redis

from app.clients.typesafe import build_client
from app.custom_indicators import CustomIndicatorRules, IndicatorRule

logger = logging.getLogger(__name__)

# CustomIndicatorRules.rule_for() is an exact lowercase dict hit, and DecisionEngine.evaluate() — which the
# backtester replays thousands of times — must stay synchronous. So matching happens once per scan, before
# evaluate(), and the result is handed over as ScanInputs.extra_rules keyed by the alert's own name.

_NO_MATCH = "none_of_these"
_CACHE_PREFIX = "indicator_rule:"
_MAX_PER_REQUEST = 8


class _SystemOneClient(Protocol):
    async def system_one(self, state: Any, questions: Any, **kwargs: Any) -> Any: ...


class IndicatorMatcher:
    """Maps a Pine alert's indicator name onto a configured rule when the name has drifted from the config key."""

    def __init__(
        self,
        client: _SystemOneClient | None,
        cache: Redis | None = None,
        min_confidence: float = 0.7,
        cache_ttl_seconds: int = 30 * 86_400,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._min_confidence = min_confidence
        self._ttl = cache_ttl_seconds
        self._model = model
        self._memo: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def resolve(
        self, alert_names: Iterable[str], rules: CustomIndicatorRules
    ) -> tuple[dict[str, IndicatorRule], list[str]]:
        """Returns (alert name -> configured rule, names still unmatched). Never raises."""
        configured = rules.names
        unknown = [n for n in dict.fromkeys(alert_names) if n and not rules.has(n)]
        if not unknown or not configured or self._client is None:
            return {}, unknown

        fingerprint = _fingerprint(configured)
        matched: dict[str, IndicatorRule] = {}
        to_ask: list[str] = []
        for name in unknown:
            cached = self._memo.get(_key(fingerprint, name)) or await self._cached(fingerprint, name)
            if cached is None:
                to_ask.append(name)
            elif cached != _NO_MATCH:
                rule = rules.named(cached)
                if rule is not None:
                    matched[name] = rule

        if to_ask:
            fresh = await self._ask(to_ask[:_MAX_PER_REQUEST], configured, rules)
            for name, verdict in fresh.items():
                self._memo[_key(fingerprint, name)] = verdict
                await self._store(fingerprint, name, verdict)
                if verdict != _NO_MATCH:
                    rule = rules.named(verdict)
                    if rule is not None:
                        matched[name] = rule

        if matched:
            logger.info("pine indicator names matched to configured rules",
                        extra={"matched": {k: v.bucket for k, v in matched.items()}})
        return matched, [n for n in unknown if n not in matched]

    async def _ask(self, names: list[str], configured: list[str], rules: CustomIndicatorRules) -> dict[str, str]:
        try:
            from typesafe_sdk import Choice
        except ImportError:  # pragma: no cover - guarded at construction
            return {}

        criteria: dict[str, Any] = {}
        for name in configured:
            rule = rules.named(name)
            if rule is None:
                continue
            what = f"The configured rule {name!r}: {rule.bucket} bucket, {rule.points:g} points"
            if rule.value is not None:
                what += f", reads the {rule.value!r} value"
            criteria[name] = {"what": what + "."}
        if not criteria:
            return {}
        criteria[_NO_MATCH] = {
            "what": "None of the configured rules describes this indicator.",
            "not_for": "A rule whose name is merely spelled differently — punctuation, spacing or a version suffix.",
        }

        state = {
            "unmatched_alert_names": names,
            "configured_rules": [{"name": n, **_rule_summary(rules.named(n))} for n in configured if rules.named(n)],
        }
        questions = {
            name: Choice(
                instructions=(
                    f"A TradingView alert arrived from an indicator calling itself {name!r}, and the scanner has "
                    "no rule under exactly that name. Which of the configured rules is this same indicator? Match "
                    "on what the indicator is, not on exact spelling: names drift through punctuation, spacing, "
                    "capitalisation and version suffixes. Answer none_of_these when it is a genuinely different "
                    "indicator."
                ),
                criteria=criteria,
            )
            for name in names
        }

        try:
            kwargs: dict[str, Any] = {"model": self._model} if self._model else {}
            response = await self._client.system_one(state=state, questions=questions, **kwargs)
        except Exception as exc:  # noqa: BLE001 - an unmatched indicator just keeps the default rule
            logger.warning("indicator matching failed", extra={"names": names, "error": str(exc)})
            return {}

        try:
            choices = response.choices
        except (AttributeError, TypeError):
            logger.warning("indicator matching response not understood", extra={"names": names})
            return {}

        out: dict[str, str] = {}
        for name in names:
            answer = choices.get(name)
            if answer is None:
                continue
            choice, confidence = str(answer.choice), float(answer.confidence)
            if choice != _NO_MATCH and (choice not in criteria or confidence < self._min_confidence):
                logger.info("indicator match rejected", extra={"alert": name, "choice": choice,
                                                               "confidence": confidence})
                out[name] = _NO_MATCH
                continue
            out[name] = choice
        return out

    async def _cached(self, fingerprint: str, name: str) -> str | None:
        if self._cache is None:
            return None
        try:
            hit = await self._cache.get(_CACHE_PREFIX + _key(fingerprint, name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("indicator match cache read failed", extra={"error": str(exc)})
            return None
        return str(hit) if hit else None

    async def _store(self, fingerprint: str, name: str, verdict: str) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(_CACHE_PREFIX + _key(fingerprint, name), verdict, ex=self._ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("indicator match cache write failed", extra={"error": str(exc)})


def _rule_summary(rule: IndicatorRule | None) -> dict[str, Any]:
    if rule is None:
        return {}
    return {"bucket": rule.bucket, "points": rule.points, "reads_value": rule.value}


def _fingerprint(configured: list[str]) -> str:
    """Cached verdicts are only valid for the rule set they were chosen from."""
    return hashlib.sha1("|".join(sorted(n.lower() for n in configured)).encode()).hexdigest()[:10]


def _key(fingerprint: str, name: str) -> str:
    return f"{fingerprint}:{name.lower()}"


def build_matcher(settings: Any, client: Any | None = None, cache: Redis | None = None) -> IndicatorMatcher | None:
    """None when indicator matching is off; a disabled matcher is never constructed."""
    if not (settings.typesafe_active and settings.typesafe_indicator_matching):
        return None
    client = client if client is not None else build_client(settings)
    if client is None:
        return None
    return IndicatorMatcher(
        client,
        cache=cache,
        min_confidence=settings.typesafe_indicator_min_confidence,
        cache_ttl_seconds=settings.typesafe_symbol_cache_ttl_seconds,
        model=settings.typesafe_model,
    )


__all__ = ["IndicatorMatcher", "build_matcher"]
