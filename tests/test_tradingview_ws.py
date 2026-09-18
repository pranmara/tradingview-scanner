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
