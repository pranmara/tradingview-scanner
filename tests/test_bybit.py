from __future__ import annotations

import asyncio

import httpx
import pytest

from app.asset_classifier import classify
from app.clients.bybit import BybitClient
from app.resilience import UpstreamError
from app.schemas import Timeframe
from app.timeframes import BYBIT_INTERVAL


def _kline(ts: int, close: float) -> list[str]:
    # v5 rows are strings: [startTime, open, high, low, close, volume, turnover]
    return [str(ts), "1.0", "2.0", "0.5", str(close), "100.0", "250.0"]


def _ok(rows: list[list[str]], category: str = "spot") -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": {"symbol": "XYZUSDT", "category": category, "list": rows}}


def _client(handler) -> BybitClient:  # noqa: ANN001
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BybitClient(http, base_url="https://bybit.test")


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


# ------------------------------------------------------------------ response shape
def test_newest_first_rows_are_returned_oldest_first():
    """v5 lists newest first; every consumer here assumes ascending time."""
    rows = [_kline(3000, 3.0), _kline(2000, 2.0), _kline(1000, 1.0)]
    out = run(_client(lambda r: httpx.Response(200, json=_ok(rows))).get_ohlcv("XYZUSDT", Timeframe.H1, 10))

    assert [c.ts for c in out.candles] == [1000, 2000, 3000]
    assert [c.close for c in out.candles] == [1.0, 2.0, 3.0]
    assert out.source == "bybit-spot"


def test_interval_and_category_are_sent_correctly():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_ok([_kline(1000, 1.0)]))

    run(_client(handler).get_ohlcv("XYZUSDT", Timeframe.H4, 300))

    assert seen["interval"] == BYBIT_INTERVAL[Timeframe.H4] == "240"
    assert seen["category"] == "spot"
    assert seen["symbol"] == "XYZUSDT"


def test_limit_is_capped_at_the_api_maximum():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_ok([_kline(1000, 1.0)]))

    run(_client(handler).get_ohlcv("XYZUSDT", Timeframe.H1, 5000))
    assert seen["limit"] == "1000"


def test_every_timeframe_has_an_interval():
    assert set(BYBIT_INTERVAL) == set(Timeframe)


# ------------------------------------------------------------------ the 200-with-an-error case
def test_a_business_error_inside_a_200_is_not_treated_as_success():
    body = {"retCode": 10001, "retMsg": "Not supported symbols", "result": {}}
    with pytest.raises(UpstreamError) as exc:
        run(_client(lambda r: httpx.Response(200, json=body)).get_ohlcv("NOPEUSDT", Timeframe.H1, 10))
    assert "10001" in str(exc.value)


def test_an_empty_list_is_not_treated_as_success():
    with pytest.raises(UpstreamError):
        run(_client(lambda r: httpx.Response(200, json=_ok([]))).get_ohlcv("XYZUSDT", Timeframe.H1, 10))


# ------------------------------------------------------------------ spot -> perp
def test_a_perp_only_token_falls_through_to_the_linear_category():
    """Newer tokens often have a perpetual months before a spot pair."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        category = request.url.params["category"]
        calls.append(category)
        if category == "spot":
            return httpx.Response(200, json={"retCode": 10001, "retMsg": "Not supported symbols", "result": {}})
        return httpx.Response(200, json=_ok([_kline(1000, 1.0)], category="linear"))

    out = run(_client(handler).get_ohlcv("XYZUSDT", Timeframe.H1, 10))

    assert calls == ["spot", "linear"]
    assert out.source == "bybit-linear"


def test_spot_wins_when_both_exist():
    out = run(_client(lambda r: httpx.Response(200, json=_ok([_kline(1000, 1.0)]))).get_ohlcv("XYZUSDT", Timeframe.H1, 10))
    assert out.source == "bybit-spot"


def test_a_token_on_neither_category_reports_both_failures():
    body = {"retCode": 10001, "retMsg": "Not supported symbols", "result": {}}
    with pytest.raises(UpstreamError) as exc:
        run(_client(lambda r: httpx.Response(200, json=body)).get_ohlcv("NOPEUSDT", Timeframe.H1, 10))
    assert "spot" in str(exc.value) and "linear" in str(exc.value)


# ------------------------------------------------------------------ paging
def test_history_pages_backwards_and_returns_ascending_candles():
    """Page size is 1000, and a short page means 'no more data' — so paging needs more than 1000 bars."""
    requested: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        end = request.url.params.get("end")
        limit = int(request.url.params["limit"])
        requested.append(int(end) if end else None)
        newest = int(end) if end else 2_000_000
        rows = [_kline(newest - 1000 * i, float(i)) for i in range(limit)]  # newest first, exactly `limit` rows
        return httpx.Response(200, json=_ok(rows))

    out = run(_client(handler).get_ohlcv_history("XYZUSDT", Timeframe.H1, 1200))

    assert len(requested) == 2 and requested[0] is None and requested[1] is not None
    assert len(out.candles) == 1200
    assert [c.ts for c in out.candles] == sorted(c.ts for c in out.candles)


def test_a_short_page_ends_the_paging_loop():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_ok([_kline(3000, 3.0), _kline(2000, 2.0)]))

    out = run(_client(handler).get_ohlcv_history("XYZUSDT", Timeframe.H1, 1200))

    assert calls["n"] == 1                       # exhausted, not re-requested forever
    assert [c.ts for c in out.candles] == [2000, 3000]


def test_history_stops_at_the_requested_bar_count():
    rows = [_kline(1000 + 100 * i, float(i)) for i in range(999, -1, -1)]  # a full newest-first page

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(rows))

    out = run(_client(handler).get_ohlcv_history("XYZUSDT", Timeframe.H1, 600))

    assert len(out.candles) == 600
    assert out.candles == sorted(out.candles, key=lambda c: c.ts)


def test_history_raises_when_nothing_comes_back():
    with pytest.raises(UpstreamError):
        run(_client(lambda r: httpx.Response(200, json=_ok([]))).get_ohlcv_history("XYZUSDT", Timeframe.H1, 100))


# ------------------------------------------------------------------ ping
def test_ping_requires_a_zero_retcode():
    assert run(_client(lambda r: httpx.Response(200, json={"retCode": 0, "result": {}})).ping()) is True
    assert run(_client(lambda r: httpx.Response(200, json={"retCode": 10002})).ping()) is False
    assert run(_client(lambda r: httpx.Response(500, text="boom")).ping()) is False


# ------------------------------------------------------------------ symbol convention
def test_binance_and_bybit_agree_on_the_pair_symbol():
    for raw in ("HYPE2", "btc/usd", "crypto:XYZ", "ETHUSDT"):
        asset = classify(f"crypto:{raw}" if ":" not in raw else raw)
        assert asset.pair_symbol == asset.binance_symbol
        assert asset.pair_symbol.endswith(("USDT", "BTC", "ETH", "EUR", "BNB"))


# ------------------------------------------------------------------ snapshot candidates
def test_crypto_snapshot_candidates_include_the_fallback_exchange(settings) -> None:
    tickers = classify("crypto:HYPE2").tradingview_symbols(("NASDAQ",), settings.crypto_exchange_candidates)
    assert tickers == ["BINANCE:HYPE2USDT", "BYBIT:HYPE2USDT"]


def test_crypto_candidates_are_ordered_and_deduplicated(settings) -> None:
    s = settings.model_copy(update={"tv_scanner_default_crypto_exchange": "BYBIT",
                                    "tv_scanner_crypto_exchange_fallbacks": "bybit, BINANCE"})
    assert s.crypto_exchange_candidates == ("BYBIT", "BINANCE")


def test_an_explicit_exchange_prefix_still_pins_to_one(settings) -> None:
    asset = classify("BYBIT:XYZUSDT")
    assert asset.tradingview_symbols(("NASDAQ",), settings.crypto_exchange_candidates) == ["BYBIT:XYZUSDT"]


def test_the_scanner_prefers_the_earliest_candidate_we_asked_for() -> None:
    """TradingView returns only what it recognises, in its own order."""
    from app.clients.tradingview_scanner import TradingViewScannerClient

    def handler(request: httpx.Request) -> httpx.Response:
        # Bybit row first, Binance second — our preference must still win.
        return httpx.Response(200, json={"data": [
            {"s": "BYBIT:BTCUSDT", "d": [2.0] * 12},
            {"s": "BINANCE:BTCUSDT", "d": [1.0] * 12},
        ]})

    client = TradingViewScannerClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    snap = run(client.get_snapshot(["BINANCE:BTCUSDT", "BYBIT:BTCUSDT"], Timeframe.H1, True))
    assert snap is not None and snap.symbol == "BINANCE:BTCUSDT"


def test_the_scanner_uses_the_fallback_when_the_first_choice_is_absent() -> None:
    from app.clients.tradingview_scanner import TradingViewScannerClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"s": "BYBIT:HYPEUSDT", "d": [2.0] * 12}]})

    client = TradingViewScannerClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    snap = run(client.get_snapshot(["BINANCE:HYPEUSDT", "BYBIT:HYPEUSDT"], Timeframe.H1, True))
    assert snap is not None and snap.symbol == "BYBIT:HYPEUSDT"
