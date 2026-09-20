from __future__ import annotations

import asyncio

import pytest

from pydantic import SecretStr

from app.command_router import (
    INTENT_CRITERIA,
    TIMEFRAME_CRITERIA,
    CommandRouter,
    build_router,
    candidate_tokens,
)
from app.schemas import Timeframe
from app.telegram_bot import strict_scan_args


class _Answer:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, choices: dict[str, _Answer], nouls: dict[str, _Answer]) -> None:
        self.choices = choices
        self.nouls = nouls


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


def _response(intent="scan", intent_c=0.9, timeframe="not_stated", timeframe_c=0.9,
              crypto="not_a_listed_crypto", crypto_c=0.9, instrument=None, instrument_c=0.9, by_name=0.05):
    choices = {
        "intent": _Answer(choice=intent, confidence=intent_c),
        "timeframe": _Answer(choice=timeframe, confidence=timeframe_c),
        "crypto_asset": _Answer(choice=crypto, confidence=crypto_c),
    }
    if instrument is not None:
        choices["instrument"] = _Answer(choice=instrument, confidence=instrument_c)
    return _Response(choices, {"names_company_not_ticker": _Answer(noul=by_name)})


def route(router: CommandRouter, text: str, **kwargs):
    return asyncio.run(router.route(text, **kwargs))


def _router(response=None, error=None, min_confidence=0.55) -> tuple[CommandRouter, FakeClient]:
    client = FakeClient(response, error)
    return CommandRouter(client, min_confidence=min_confidence), client


# ------------------------------------------------------------------ the fast path never reaches the model
def test_strict_form_parses_in_code():
    assert strict_scan_args(["BTCUSDT", "4h"]) == ("BTCUSDT", Timeframe.H4)
    assert strict_scan_args(["AAPL"]) == ("AAPL", Timeframe.H4)
    assert strict_scan_args(["BINANCE:SOLUSDT", "1w"]) == ("BINANCE:SOLUSDT", Timeframe.W1)


def test_strict_form_declines_anything_it_cannot_parse():
    assert strict_scan_args([]) is None
    assert strict_scan_args(["BTCUSDT", "bogus"]) is None
    assert strict_scan_args(["is", "btc", "worth", "a", "long"]) is None
    assert strict_scan_args(["BTCUSDT", "4h", "please"]) is None


# ------------------------------------------------------------------ candidate extraction
def test_candidates_keep_tickers_and_drop_filler():
    assert candidate_tokens("is btc worth a long on the 4h") == ["BTC"]
    assert candidate_tokens("what do you make of NVDA daily") == ["NVDA"]
    assert candidate_tokens("check BINANCE:SOLUSDT weekly") == ["BINANCE:SOLUSDT"]


def test_candidates_drop_timeframes_and_bare_numbers():
    tokens = candidate_tokens("ETH 15m 240 1d")
    assert tokens == ["ETH"]


def test_candidates_are_deduplicated_and_capped():
    assert candidate_tokens("eth ETH Eth") == ["ETH"]
    assert len(candidate_tokens(" ".join(f"TICK{i}" for i in range(40)))) == 20


# ------------------------------------------------------------------ routing
def test_crypto_is_resolved_by_spoken_name():
    router, client = _router(_response(crypto="BTC", timeframe="4h", instrument="BITCOIN"))

    r = route(router, "is bitcoin worth a long on the 4h")

    assert r.scannable
    assert r.symbol == "BTC"
    assert r.timeframe is Timeframe.H4


def test_stock_falls_through_to_the_token_the_user_typed():
    router, _ = _router(_response(intent="scan", timeframe="1d", instrument="NVDA"))

    r = route(router, "what do you make of NVDA daily")

    assert r.scannable
    assert r.symbol == "NVDA"
    assert r.timeframe is Timeframe.D1


def test_unstated_timeframe_uses_the_default():
    router, _ = _router(_response(crypto="ETH", timeframe="not_stated"))

    r = route(router, "eth setup?")

    assert r.timeframe is Timeframe.H4
    assert route(router, "eth setup?", default_timeframe=Timeframe.D1).timeframe is Timeframe.D1


def test_low_confidence_timeframe_uses_the_default_rather_than_guessing():
    router, _ = _router(_response(crypto="ETH", timeframe="5m", timeframe_c=0.2))

    assert route(router, "eth quick look").timeframe is Timeframe.H4


def test_low_confidence_intent_is_reported_as_unsure():
    router, _ = _router(_response(intent="scan", intent_c=0.3))

    r = route(router, "hmm")

    assert r.intent == "unsure"
    assert not r.scannable


def test_non_scan_intent_carries_no_arguments():
    router, _ = _router(_response(intent="status", crypto="BTC"))

    r = route(router, "are the feeds up")

    assert r.intent == "status"
    assert r.symbol is None and not r.scannable


def test_company_name_without_a_ticker_asks_for_the_ticker():
    router, _ = _router(_response(intent="scan", instrument=None, by_name=0.92))

    r = route(router, "how does apple look")

    assert not r.scannable
    assert "ticker" in r.note


def test_low_confidence_crypto_answer_does_not_pick_a_symbol():
    router, _ = _router(_response(crypto="TON", crypto_c=0.2, instrument=None, by_name=0.05))

    r = route(router, "thoughts?")

    assert not r.scannable
    assert r.note


def test_a_ticker_the_user_never_typed_is_refused():
    # The instrument must be a span from the message; the model cannot introduce a ticker of its own.
    router, _ = _router(_response(instrument="DOGE"))

    r = route(router, "what about NVDA")

    assert not r.scannable


def test_instrument_that_classify_rejects_is_refused():
    router, _ = _router(_response(instrument="A" * 25))

    r = route(router, "A" * 25)

    assert not r.scannable


def test_empty_message_never_calls_out():
    router, client = _router(_response())

    assert route(router, "   ").intent == "other"
    assert client.calls == []


def test_upstream_failure_is_reported_not_raised():
    router, _ = _router(error=RuntimeError("boom"))

    r = route(router, "is btc worth a long")

    assert r.intent == "unavailable"
    assert r.note == "RuntimeError: boom"


def test_the_note_carries_the_api_message_not_just_the_exception_class():
    """A 400 naming the offending model is the whole diagnosis; the class alone says only 'something broke'."""
    router, _ = _router(error=RuntimeError("400 Unknown model: # blank = the SDK/account default (Jev)"))

    note = route(router, "is btc worth a long").note

    assert "Unknown model" in note
    assert "RuntimeError" in note


def test_malformed_response_is_reported_not_raised():
    router, _ = _router(object())

    assert route(router, "is btc worth a long").intent == "unavailable"


def test_unknown_intent_is_treated_as_other():
    router, _ = _router(_response(intent="place_an_order"))

    assert route(router, "buy me 3 btc").intent == "other"


def test_disabled_router_reports_instead_of_calling_out():
    router = CommandRouter(None)

    assert not router.enabled
    assert route(router, "is btc worth a long").intent == "unavailable"


# ------------------------------------------------------------------ question construction
def test_every_question_is_asked_in_one_request():
    router, client = _router(_response(instrument="BTC"))

    route(router, "is btc worth a long on the 4h")

    assert len(client.calls) == 1
    assert set(client.calls[0]["questions"]) == {"intent", "timeframe", "crypto_asset",
                                                 "names_company_not_ticker", "instrument"}


def test_instrument_question_is_omitted_when_no_word_could_be_one():
    router, client = _router(_response())

    route(router, "up or down?")

    assert "instrument" not in client.calls[0]["questions"]


def test_instrument_options_are_exactly_the_words_in_the_message():
    router, client = _router(_response(instrument="NVDA"))

    route(router, "what do you make of NVDA daily")

    criteria = client.calls[0]["questions"]["instrument"].criteria
    assert set(criteria) == {"NVDA", "none_named"}


def test_timeframe_options_cover_every_supported_timeframe():
    assert {tf.value for tf in Timeframe} <= set(TIMEFRAME_CRITERIA)
    assert "not_stated" in TIMEFRAME_CRITERIA


def test_intent_options_cover_the_bot_commands():
    assert set(INTENT_CRITERIA) == {"scan", "status", "indicators", "help", "other"}


# ------------------------------------------------------------------ wiring
def test_no_router_without_a_key(settings):
    assert build_router(settings) is None


def test_no_router_when_natural_language_is_off(settings):
    off = settings.model_copy(update={"typesafe_api_key": SecretStr("sk-test"), "typesafe_natural_language": False})
    assert build_router(off) is None


def test_router_built_when_enabled(settings):
    on = settings.model_copy(update={"typesafe_api_key": SecretStr("sk-test")})
    built = build_router(on)
    assert built is not None and built.enabled
    asyncio.run(_close(built))


async def _close(router: CommandRouter) -> None:
    from app.clients.typesafe import aclose

    await aclose(router._client)  # noqa: SLF001 - test cleanup


@pytest.mark.parametrize("intent", ["status", "indicators", "help", "other"])
def test_all_non_scan_intents_pass_through(intent):
    router, _ = _router(_response(intent=intent))

    assert route(router, "something").intent == intent
