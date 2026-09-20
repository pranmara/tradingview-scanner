from __future__ import annotations

import asyncio

from pydantic import SecretStr

from app.asset_classifier import classify
from app.asset_resolver import CLASS_CRITERIA, AssetResolver, build_resolver
from app.schemas import AssetClass


class _Answer:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, choice: str, confidence: float) -> None:
        self.choices = {"asset_class": _Answer(choice=choice, confidence=confidence)}


class FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def system_one(self, state, questions, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"state": state, "questions": questions})
        if self._error is not None:
            raise self._error
        return self._response


class FakeRedis:
    def __init__(self, seed: dict[str, str] | None = None, fail: bool = False) -> None:
        self.store = dict(seed or {})
        self.fail = fail
        self.reads = 0
        self.writes: list[tuple[str, str]] = []

    async def get(self, key: str):  # noqa: ANN201
        self.reads += 1
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):  # noqa: ANN201
        if self.fail:
            raise RuntimeError("redis down")
        self.writes.append((key, value))
        self.store[key] = value


def resolve(resolver: AssetResolver, raw: str):  # noqa: ANN201
    return asyncio.run(resolver.resolve(classify(raw)))


def _resolver(choice="crypto", confidence=0.9, error=None, cache=None, min_confidence=0.7):
    client = FakeClient(None if error else _Response(choice, confidence), error)
    return AssetResolver(client, cache=cache, min_confidence=min_confidence), client


# ------------------------------------------------------------------ what counts as ambiguous
def test_deterministic_classifications_are_not_ambiguous():
    assert not classify("BTCUSDT").ambiguous          # known quote asset
    assert not classify("NASDAQ:TSLA").ambiguous      # stock exchange prefix
    assert not classify("BINANCE:SOLUSDT").ambiguous  # crypto exchange prefix
    assert not classify("stock:LINK").ambiguous       # forced by the user
    assert not classify("crypto:XYZ").ambiguous
    assert not classify("ETH").ambiguous              # in the known crypto list


def test_bare_unknown_ticker_is_ambiguous():
    assert classify("HYPE2").ambiguous
    assert classify("AAPL").ambiguous
    assert classify("XYZUSD").ambiguous  # USD pair whose base is not a known coin


# ------------------------------------------------------------------ resolution
def test_untouched_when_the_static_rules_already_decided():
    resolver, client = _resolver()

    out = resolve(resolver, "BTCUSDT")

    assert out.asset_class is AssetClass.CRYPTO
    assert client.calls == []


def test_confident_crypto_verdict_reclassifies_the_symbol():
    resolver, _ = _resolver(choice="crypto", confidence=0.93)

    out = resolve(resolver, "HYPE2")

    assert out.asset_class is AssetClass.CRYPTO
    assert out.symbol == "HYPE2USDT"
    assert out.binance_symbol == "HYPE2USDT"


def test_usd_pair_keeps_its_quote_when_resolved_to_crypto():
    resolver, _ = _resolver(choice="crypto", confidence=0.93)

    out = resolve(resolver, "XYZUSD")

    assert out.asset_class is AssetClass.CRYPTO
    assert (out.base, out.quote) == ("XYZ", "USD")


def test_stock_verdict_keeps_the_existing_fallback():
    resolver, _ = _resolver(choice="stock", confidence=0.95)

    out = resolve(resolver, "AAPL")

    assert out.asset_class is AssetClass.STOCK
    assert out.symbol == "AAPL"


def test_unclear_verdict_keeps_the_existing_fallback():
    resolver, _ = _resolver(choice="unclear", confidence=0.95)

    assert resolve(resolver, "HYPE2").asset_class is AssetClass.STOCK


def test_low_confidence_never_redirects_the_market():
    resolver, _ = _resolver(choice="crypto", confidence=0.4, min_confidence=0.7)

    assert resolve(resolver, "HYPE2").asset_class is AssetClass.STOCK


def test_upstream_failure_keeps_the_existing_fallback():
    resolver, _ = _resolver(error=RuntimeError("boom"))

    assert resolve(resolver, "HYPE2").asset_class is AssetClass.STOCK


def test_malformed_response_keeps_the_existing_fallback():
    resolver = AssetResolver(FakeClient(object()))

    assert resolve(resolver, "HYPE2").asset_class is AssetClass.STOCK


def test_disabled_resolver_passes_everything_through():
    resolver = AssetResolver(None)

    assert not resolver.enabled
    assert resolve(resolver, "HYPE2").asset_class is AssetClass.STOCK


# ------------------------------------------------------------------ caching: one call per ticker, ever
def test_second_scan_of_the_same_ticker_does_not_call_out():
    resolver, client = _resolver(choice="crypto", confidence=0.9)

    resolve(resolver, "HYPE2")
    out = resolve(resolver, "HYPE2")

    assert len(client.calls) == 1
    assert out.asset_class is AssetClass.CRYPTO


def test_an_unresolved_answer_is_remembered_too():
    # "unclear" is a real answer; re-asking it on every scan would be pure waste.
    resolver, client = _resolver(choice="unclear", confidence=0.9)

    resolve(resolver, "HYPE2")
    resolve(resolver, "HYPE2")

    assert len(client.calls) == 1


def test_a_failed_lookup_is_retried_next_time():
    resolver, client = _resolver(error=RuntimeError("boom"))

    resolve(resolver, "HYPE2")
    resolve(resolver, "HYPE2")

    assert len(client.calls) == 2


def test_redis_hit_skips_the_request_entirely():
    cache = FakeRedis({"asset_class:HYPE2": "crypto"})
    resolver, client = _resolver(cache=cache)

    out = resolve(resolver, "HYPE2")

    assert client.calls == []
    assert out.asset_class is AssetClass.CRYPTO


def test_a_verdict_is_written_to_redis():
    cache = FakeRedis()
    resolver, _ = _resolver(choice="crypto", confidence=0.9, cache=cache)

    resolve(resolver, "HYPE2")

    assert cache.writes == [("asset_class:HYPE2", "crypto")]


def test_a_broken_cache_does_not_break_resolution():
    resolver, _ = _resolver(choice="crypto", confidence=0.9, cache=FakeRedis(fail=True))

    assert resolve(resolver, "HYPE2").asset_class is AssetClass.CRYPTO


# ------------------------------------------------------------------ question construction
def test_state_carries_the_split_the_classifier_already_computed():
    resolver, client = _resolver()

    resolve(resolver, "XYZUSD")

    state = client.calls[0]["state"]
    assert state["ticker"] == "XYZUSD"
    assert state["base"] == "XYZ"
    assert state["trailing_quote_asset"] == "USD"
    assert state["in_scanners_known_crypto_list"] is False


def test_one_question_per_lookup():
    resolver, client = _resolver()

    resolve(resolver, "HYPE2")

    assert list(client.calls[0]["questions"]) == ["asset_class"]


def test_options_include_a_no_match_outcome():
    assert set(CLASS_CRITERIA) == {"crypto", "stock", "unclear"}


# ------------------------------------------------------------------ wiring
def test_no_resolver_without_a_key(settings):
    assert build_resolver(settings) is None


def test_no_resolver_when_symbol_resolution_is_off(settings):
    off = settings.model_copy(update={"typesafe_api_key": SecretStr("sk-test"),
                                      "typesafe_symbol_resolution": False})
    assert build_resolver(off) is None


def test_resolver_built_when_enabled(settings):
    on = settings.model_copy(update={"typesafe_api_key": SecretStr("sk-test")})
    built = build_resolver(on)
    assert built is not None and built.enabled

    from app.clients.typesafe import aclose

    asyncio.run(aclose(built._client))  # noqa: SLF001 - test cleanup
