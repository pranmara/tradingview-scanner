from __future__ import annotations

import logging
from typing import Any, Protocol

from redis.asyncio import Redis

from app.asset_classifier import KNOWN_CRYPTO_BASES, QUOTE_ASSETS, AssetInfo, classify
from app.clients.typesafe import build_client

logger = logging.getLogger(__name__)

# classify() is synchronous and sits on the webhook hot path and in the backtester, so it stays pure: it reports
# `ambiguous` and keeps its existing STOCK fallback. Only the scan path, which can afford one lookup, resolves it.

CLASS_CRITERIA: dict[str, Any] = {
    "crypto": {
        "what": "A cryptocurrency, token or coin, traded on a crypto exchange against USDT, USD or BTC.",
        "examples": ["HYPE", "KAS", "ONDO", "TIA", "PENGU"],
    },
    "stock": {
        "what": "An equity, ETF, index or fund traded on a stock exchange such as NASDAQ, NYSE, LSE or NSE.",
        "examples": ["AAPL", "NVDA", "SPY", "BRK.B", "RELIANCE"],
    },
    "unclear": {
        "what": "The ticker is used by both a well-known token and a listed equity, or it is not a ticker you recognise at all.",
        "not_for": "A ticker that is merely obscure but clearly belongs to one of the two worlds.",
    },
}

_CACHE_PREFIX = "asset_class:"


class _SystemOneClient(Protocol):
    async def system_one(self, state: Any, questions: Any, **kwargs: Any) -> Any: ...


class AssetResolver:
    """Decides crypto-vs-stock for a bare ticker the deterministic rules could not place."""

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

    async def resolve(self, asset: AssetInfo) -> AssetInfo:
        """Returns `asset` unchanged unless the answer is confident and differs from the fallback."""
        if self._client is None or not asset.ambiguous:
            return asset

        key = asset.symbol.upper()
        verdict = self._memo.get(key) or await self._cached(key)
        if verdict is None:
            verdict = await self._ask(asset)
            if verdict is not None:
                self._memo[key] = verdict
                await self._store(key, verdict)
        if verdict != "crypto":
            return asset

        resolved = classify(f"crypto:{asset.raw}")
        logger.info("ambiguous ticker resolved to crypto",
                    extra={"raw": asset.raw, "was": asset.symbol, "now": resolved.symbol})
        return resolved

    async def _ask(self, asset: AssetInfo) -> str | None:
        try:
            from typesafe_sdk import Choice
        except ImportError:  # pragma: no cover - guarded at construction
            return None

        base, quote = _split_known_quote(asset.symbol)
        state = {
            "ticker": asset.symbol,
            "as_typed": asset.raw,
            "base": base,
            "trailing_quote_asset": quote,
            "in_scanners_known_crypto_list": base in KNOWN_CRYPTO_BASES,
        }
        question = Choice(
            instructions=(
                "A chart scanner was given the ticker `ticker` with no exchange prefix, so it cannot tell which "
                "market to load it from. Is this a cryptocurrency or a listed equity? `base` is the ticker with any "
                "trailing quote asset removed, and `trailing_quote_asset` is that suffix when there was one."
            ),
            criteria=dict(CLASS_CRITERIA),
        )

        try:
            kwargs: dict[str, Any] = {"model": self._model} if self._model else {}
            response = await self._client.system_one(state=state, questions={"asset_class": question}, **kwargs)
        except Exception as exc:  # noqa: BLE001 - an unresolved ticker just keeps the existing fallback
            logger.warning("asset class resolution failed", extra={"ticker": asset.symbol, "error": str(exc)})
            return None

        try:
            answer = response.choices["asset_class"]
        except (AttributeError, KeyError, TypeError):
            logger.warning("asset class response not understood", extra={"ticker": asset.symbol})
            return None

        choice, confidence = str(answer.choice), float(answer.confidence)
        if choice not in CLASS_CRITERIA or confidence < self._min_confidence:
            logger.info("asset class left unresolved", extra={"ticker": asset.symbol, "choice": choice,
                                                              "confidence": confidence})
            return "unclear"
        return choice

    async def _cached(self, key: str) -> str | None:
        if self._cache is None:
            return None
        try:
            hit = await self._cache.get(_CACHE_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("asset class cache read failed", extra={"error": str(exc)})
            return None
        return str(hit) if hit else None

    async def _store(self, key: str, verdict: str) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(_CACHE_PREFIX + key, verdict, ex=self._ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("asset class cache write failed", extra={"error": str(exc)})


def _split_known_quote(symbol: str) -> tuple[str, str | None]:
    for quote in QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote) + 1:
            return symbol[: -len(quote)], quote
    return symbol, None


def build_resolver(settings: Any, client: Any | None = None, cache: Redis | None = None) -> AssetResolver | None:
    """None when symbol resolution is off; a disabled resolver is never constructed."""
    if not (settings.typesafe_active and settings.typesafe_symbol_resolution):
        return None
    client = client if client is not None else build_client(settings)
    if client is None:
        return None
    return AssetResolver(
        client,
        cache=cache,
        min_confidence=settings.typesafe_symbol_min_confidence,
        cache_ttl_seconds=settings.typesafe_symbol_cache_ttl_seconds,
        model=settings.typesafe_model,
    )


__all__ = ["AssetResolver", "CLASS_CRITERIA", "build_resolver"]
