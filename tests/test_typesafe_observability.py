from __future__ import annotations

import asyncio
import json
import logging

from app.asset_resolver import AssetResolver
from app.clients.typesafe import describe_error, log_call
from app.command_router import CommandRouter
from app.logging_config import JsonFormatter
from tests.test_asset_resolver import FakeClient as ResolverClient
from tests.test_asset_resolver import _Response as ResolverResponse
from tests.test_asset_resolver import resolve as resolve_asset
from tests.test_command_router import FakeClient as RouterClient
from tests.test_command_router import _response as router_response
from tests.test_command_router import route as run_route


def _calls(caplog) -> list[dict]:  # noqa: ANN001
    return [r.__dict__ for r in caplog.records if r.getMessage() == "typesafe call"]


# ------------------------------------------------------------------ describe_error
def test_error_description_keeps_the_api_message():
    # The real incident: only the API's text named the offending model.
    exc = RuntimeError("POST https://api.typesafe.ai/v1/systemone: 400 Unknown model: # blank = the default")

    described = describe_error(exc)

    assert described.startswith("RuntimeError: ")
    assert "400 Unknown model" in described


def test_error_description_falls_back_to_the_class_when_there_is_no_message():
    assert describe_error(ValueError()) == "ValueError"


def test_error_description_is_capped():
    assert len(describe_error(RuntimeError("x" * 900), limit=50)) <= len("RuntimeError: ") + 50


# ------------------------------------------------------------------ log_call
def test_a_logged_call_survives_the_json_formatter(caplog):
    with caplog.at_level(logging.INFO, logger="app.clients.typesafe"):
        log_call("autoconfig", 0.0, "applied", script="MyOsc", confidence=0.91)

    record = next(r for r in caplog.records if r.getMessage() == "typesafe call")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["feature"] == "autoconfig"
    assert payload["outcome"] == "applied"
    assert payload["script"] == "MyOsc"
    assert isinstance(payload["ms"], int)


# ------------------------------------------------------------------ every request logs, whatever happens
def test_a_successful_route_is_logged(caplog):
    router = CommandRouter(RouterClient(router_response(intent="scan", crypto="BTC")), min_confidence=0.55)

    with caplog.at_level(logging.INFO):
        run_route(router, "is btc worth a long")

    call = _calls(caplog)[0]
    assert call["feature"] == "natural_language"
    assert call["outcome"] == "scan"
    assert call["resolved_symbol"] == "BTC"


def test_a_failed_route_is_logged_with_the_reason(caplog):
    router = CommandRouter(RouterClient(error=RuntimeError("400 Unknown model: nope")), min_confidence=0.55)

    with caplog.at_level(logging.INFO):
        run_route(router, "is btc worth a long")

    call = _calls(caplog)[0]
    assert call["outcome"] == "error"
    assert "Unknown model" in call["error"]


def test_a_stock_verdict_is_logged_even_though_nothing_changes(caplog):
    """The gap that made this invisible: a confident 'stock' answer used to leave no trace at all."""
    resolver = AssetResolver(ResolverClient(ResolverResponse("stock", 0.95)), min_confidence=0.7)

    with caplog.at_level(logging.INFO):
        out = resolve_asset(resolver, "AAPL")

    call = _calls(caplog)[0]
    assert call["feature"] == "symbol_resolution"
    assert call["outcome"] == "stock"
    assert call["accepted"] is True
    assert out.symbol == "AAPL"  # unchanged, but no longer silent


def test_a_rejected_verdict_records_what_was_said_and_why(caplog):
    resolver = AssetResolver(ResolverClient(ResolverResponse("crypto", 0.41)), min_confidence=0.7)

    with caplog.at_level(logging.INFO):
        resolve_asset(resolver, "HYPE2")

    call = _calls(caplog)[0]
    assert call["outcome"] == "unclear"
    assert call["said"] == "crypto"
    assert call["accepted"] is False
    assert call["confidence"] == 0.41


def test_a_cache_hit_does_not_pretend_a_call_was_made(caplog):
    resolver = AssetResolver(ResolverClient(ResolverResponse("crypto", 0.95)), min_confidence=0.7)
    resolve_asset(resolver, "HYPE2")

    with caplog.at_level(logging.INFO):
        resolve_asset(resolver, "HYPE2")

    assert _calls(caplog) == []


def test_every_feature_logs_under_its_own_name():
    from app import asset_resolver, command_router, indicator_matcher, study_advisor

    sources = " ".join(
        __import__("pathlib").Path(m.__file__).read_text(encoding="utf-8")
        for m in (asset_resolver, command_router, indicator_matcher, study_advisor)
    )
    for feature in ("natural_language", "symbol_resolution", "indicator_matching", "autoconfig"):
        assert f'log_call("{feature}"' in sources


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)
