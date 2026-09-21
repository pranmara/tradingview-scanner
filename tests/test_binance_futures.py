from __future__ import annotations

import asyncio

import httpx
import pytest

from app.clients.binance_futures import BinanceFuturesClient
from app.resilience import UpstreamError


def _client(handler) -> BinanceFuturesClient:  # noqa: ANN001
    return BinanceFuturesClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="https://fapi.test")


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def _funding(ts: int, rate: float) -> dict:
    return {"symbol": "XYZUSDT", "fundingTime": ts, "fundingRate": str(rate), "markPrice": "1.0"}


def _kline(ts: int, close: float) -> list:
    return [ts, "1", "2", "0.5", str(close), "100", ts + 1, "0", 0, "0", "0", "0"]


# ------------------------------------------------------------------ funding
def test_funding_parses_rates_as_floats_in_ascending_order():
    out = run(_client(lambda r: httpx.Response(200, json=[_funding(1000, 0.0001), _funding(2000, -0.0002)]))
              .funding_history("XYZUSDT", 0))
    assert [(p.ts, p.rate) for p in out] == [(1000, 0.0001), (2000, -0.0002)]


def test_funding_pages_forward_from_the_last_timestamp():
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        starts.append(start)
        if start == 0:  # a full page means "there may be more"
            return httpx.Response(200, json=[_funding(1000 + i, 0.0001) for i in range(1000)])
        return httpx.Response(200, json=[_funding(5000, 0.0003)])

    out = run(_client(handler).funding_history("XYZUSDT", 0))

    assert starts == [0, 1000 + 999 + 1]   # resumes one ms after the last record, never re-reads it
    assert len(out) == 1001 and out[-1].rate == 0.0003


def test_funding_passes_the_end_bound_through():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    run(_client(handler).funding_history("XYZUSDT", 0, end_ms=9999))
    assert seen["endTime"] == "9999"


def test_an_empty_funding_history_is_empty_not_an_error():
    assert run(_client(lambda r: httpx.Response(200, json=[])).funding_history("XYZUSDT", 0)) == []


def test_overlapping_pages_do_not_duplicate_records():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[_funding(1000 + i, 0.0) for i in range(1000)])
        return httpx.Response(200, json=[_funding(1999, 0.0), _funding(2500, 0.0)])  # 1999 already seen

    out = run(_client(handler).funding_history("XYZUSDT", 0))
    assert [p.ts for p in out].count(1999) == 1 and out[-1].ts == 2500


# ------------------------------------------------------------------ klines
def test_klines_parse_and_page():
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        starts.append(start)
        if start == 0:
            return httpx.Response(200, json=[_kline(100 * i, float(i)) for i in range(1500)])
        return httpx.Response(200, json=[_kline(10**7, 9.0)])

    out = run(_client(handler).klines("XYZUSDT", "1d", 0))
    assert len(starts) == 2 and len(out) == 1501
    assert out == sorted(out, key=lambda c: c.ts)


def test_no_klines_is_an_error():
    with pytest.raises(UpstreamError):
        run(_client(lambda r: httpx.Response(200, json=[])).klines("XYZUSDT", "1d", 0))


# ------------------------------------------------------------------ universe
def test_universe_is_live_usdt_perpetuals_ranked_by_volume():
    info = {"symbols": [
        {"symbol": "AUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "BUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "CUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "SETTLING"},     # not live
        {"symbol": "DUSDT_260925", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "EUSDC", "contractType": "PERPETUAL", "quoteAsset": "USDC", "status": "TRADING"},      # wrong quote
    ]}
    tickers = [{"symbol": "AUSDT", "quoteVolume": "10"}, {"symbol": "BUSDT", "quoteVolume": "90"},
               {"symbol": "CUSDT", "quoteVolume": "999"}, {"symbol": "EUSDC", "quoteVolume": "999"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=info if request.url.path.endswith("exchangeInfo") else tickers)

    assert run(_client(handler).usdt_perpetuals_by_volume(10)) == ["BUSDT", "AUSDT"]
