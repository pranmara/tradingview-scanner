from __future__ import annotations

import json

from app.clients.tradingview_ws import TradingViewSessionClient, _pack


def test_pack_frames_message() -> None:
    frame = _pack("set_auth_token", ["tok"])
    body = json.dumps({"m": "set_auth_token", "p": ["tok"]}, separators=(",", ":"))
    assert frame == f"~m~{len(body)}~m~{body}"


def test_split_frames_and_heartbeat() -> None:
    raw = '~m~4~m~~h~1~m~20~m~{"m":"x","p":[1,2,3]}'
    frames = TradingViewSessionClient._frames(raw)
    assert frames == ["~h~1", '{"m":"x","p":[1,2,3]}']


def test_study_inputs_and_plot_names() -> None:
    meta = {
        "ilTemplate": "SCRIPT",
        "metaInfo": {
            "pine": {"version": "2.0"},
            "inputs": [
                {"id": "in_0", "name": "Length", "defval": 14, "type": "integer"},
                {"id": "in_1", "name": "Source", "defval": "close", "type": "source"},
                {"id": "text", "defval": "ignored"},
            ],
            "plots": [{"id": "plot_0"}, {"id": "plot_1"}],
            "styles": {"plot_0": {"title": "Fast Line"}, "plot_1": {"title": "Slow/Line 2"}},
        },
    }
    inputs, plots = TradingViewSessionClient._study_inputs(meta, {"Length": 21}, "PUB;abc")
    assert inputs["text"] == "SCRIPT" and inputs["pineId"] == "PUB;abc" and inputs["pineVersion"] == "2.0"
    assert inputs["in_0"] == {"v": 21, "f": False, "t": "integer"}
    assert inputs["in_1"]["v"] == "close"
    assert "text" in inputs and inputs["text"] == "SCRIPT"
    assert plots == ["Fast_Line", "Slow_Line_2"]


def test_a_study_that_never_completes_times_out_instead_of_hanging(monkeypatch) -> None:
    """Heartbeats arrive forever and reset the per-message timeout; the session deadline must still fire."""
    import asyncio
    import time

    import httpx
    import pytest

    from app.clients import tradingview_ws as tvws
    from app.resilience import UpstreamError
    from app.schemas import Timeframe

    class HeartbeatOnly:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        async def send(self, msg: str) -> None:
            self.sent.append(msg)

        async def recv(self) -> str:
            await asyncio.sleep(0.01)
            # series completes, then the server only ever heartbeats: no study_completed, no study_error
            if not any("series_completed" in m for m in self.sent) and len(self.sent) > 3:
                self.sent.append("series_completed")
                return '~m~40~m~{"m":"series_completed","p":["cs","sds_1"]}'
            return "~m~4~m~~h~1"

    monkeypatch.setattr(tvws.websockets, "connect", lambda *a, **k: HeartbeatOnly())
    client = tvws.TradingViewSessionClient(httpx.AsyncClient(), timeout_seconds=0.05)

    async def fake_token() -> str:
        return "tok"

    monkeypatch.setattr(client, "auth_token", fake_token)
    started = time.monotonic()
    with pytest.raises(UpstreamError, match="did not complete"):
        asyncio.run(client._session("BINANCE:BTCUSDT", Timeframe.H4, 10, ("PUB;x", {})))
    assert time.monotonic() - started < 5   # bounded by the session deadline, not forever
