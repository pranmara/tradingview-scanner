from __future__ import annotations

import json
from pathlib import Path

from app.clients.tradingview_ws import ScriptInfo
from app.study_registry import StudyRegistry, parse_kv


def test_parse_kv_splits_rule_and_inputs() -> None:
    rule, inputs = parse_kv(["plot=osc", "above=55", "below=45", "points=8", "in.Length=20", "in.Source=hlc3", "junk"])
    assert rule == {"plot": "osc", "above": 55, "below": 45, "points": 8}
    assert inputs == {"Length": 20, "Source": "hlc3"}


def test_registry_add_resolve_remove_persist(tmp_path: Path) -> None:
    path = tmp_path / "active.json"
    reg = StudyRegistry(str(path))
    reg._listing = [ScriptInfo("USER;abc", "My Private Osc", "1", "study", "saved")]  # simulate /indicators list
    script = reg.resolve("1")
    assert script is not None and script.pine_id == "USER;abc"
    assert reg.resolve("my private osc") is script
    assert reg.resolve("PUB;zzz") is not None and reg.resolve("nope") is None

    name, rule = reg.add(script, {"plot": "osc", "above": 55, "below": 45, "points": 8}, {"Length": 20})
    assert name == "My_Private_Osc" and rule.value == "osc" and rule.bullish_above == 55 and rule.points == 8
    assert reg.active()[name]["inputs"] == {"Length": 20}
    assert reg.extra_rules()[name].bucket == "indicators"

    reloaded = StudyRegistry(str(path))
    assert set(reloaded.active()) == {name}
    assert reloaded.remove("my private osc") and reloaded.active() == {}
    assert json.loads(path.read_text()) == {}


def test_registry_seeds_from_config(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"_comment": "x", "Osc": {"pine_id": "USER;1", "inputs": {}, "rule": {"value": "plot_0", "bullish_above": 0, "bearish_below": 0}}}))
    reg = StudyRegistry(str(tmp_path / "active.json"), str(seed))
    assert list(reg.active()) == ["Osc"] and reg.extra_rules()["Osc"].value == "plot_0"
