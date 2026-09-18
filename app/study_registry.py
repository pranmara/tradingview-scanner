from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.clients.tradingview_ws import ScriptInfo, TradingViewSessionClient
from app.custom_indicators import IndicatorRule

logger = logging.getLogger(__name__)

_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _coerce(v: str) -> Any:
    if _NUM.match(v):
        return float(v) if "." in v else int(v)
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def parse_kv(tokens: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split `key=value` tokens into (rule options, study inputs). Inputs are prefixed `in.`: in.Length=20."""
    rule: dict[str, Any] = {}
    inputs: dict[str, Any] = {}
    for tok in tokens:
        key, sep, val = tok.partition("=")
        if not sep:
            continue
        if key.startswith("in."):
            inputs[key[3:]] = _coerce(val)
        else:
            rule[key.lower()] = _coerce(val)
    return rule, inputs


class StudyRegistry:
    """Account indicators selected for scans. Persisted to a writable JSON file, seeded from config/tv_studies.json."""

    def __init__(self, path: str, seed_path: str | None = None) -> None:
        self._path = Path(path)
        self._active: dict[str, dict[str, Any]] = {}
        self._listing: list[ScriptInfo] = []
        source = self._path if self._path.exists() else (Path(seed_path) if seed_path and Path(seed_path).exists() else None)
        if source is not None:
            try:
                raw = json.loads(source.read_text(encoding="utf-8"))
                self._active = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict) and v.get("pine_id")}
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("study registry unreadable", extra={"path": str(source), "error": str(exc)})

    # ------------------------------------------------------------------ state
    def active(self) -> dict[str, dict[str, Any]]:
        return dict(self._active)

    @property
    def listing(self) -> list[ScriptInfo]:
        return list(self._listing)

    def extra_rules(self) -> dict[str, IndicatorRule]:
        out: dict[str, IndicatorRule] = {}
        for name, spec in self._active.items():
            rule = spec.get("rule")
            if not isinstance(rule, dict):
                continue
            try:
                out[name] = IndicatorRule.model_validate(rule)
            except ValidationError as exc:
                logger.warning("invalid study rule ignored", extra={"study": name, "error": str(exc)})
        return out

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._active, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error("study registry write failed", extra={"path": str(self._path), "error": str(exc)})

    # ------------------------------------------------------------------ discovery
    async def refresh(self, tv: TradingViewSessionClient) -> list[ScriptInfo]:
        self._listing = await tv.list_scripts()
        return self.listing

    def resolve(self, target: str) -> ScriptInfo | None:
        t = target.strip()
        if t.isdigit():
            idx = int(t) - 1
            return self._listing[idx] if 0 <= idx < len(self._listing) else None
        for s in self._listing:
            if s.pine_id == t or s.name.lower() == t.lower():
                return s
        if ";" in t:
            return ScriptInfo(pine_id=t, name=t.split(";", 1)[1][:24], version="last", kind="study", source="manual")
        return None

    # ------------------------------------------------------------------ mutation
    def add(self, script: ScriptInfo, rule_opts: dict[str, Any], inputs: dict[str, Any], alias: str | None = None) -> tuple[str, IndicatorRule]:
        name = re.sub(r"[^A-Za-z0-9_]+", "_", alias or script.name).strip("_") or script.pine_id
        rule = IndicatorRule(
            bucket=str(rule_opts.get("bucket", "indicators")),  # type: ignore[arg-type]
            points=float(rule_opts.get("points", 5)),
            max_age_bars=int(rule_opts.get("age", 2)),
            value=str(rule_opts.get("plot", "plot_0")),
            bullish_above=float(rule_opts["above"]) if "above" in rule_opts else 0.0,
            bearish_below=float(rule_opts["below"]) if "below" in rule_opts else 0.0,
        )
        self._active[name] = {
            "pine_id": script.pine_id, "display": script.name, "inputs": inputs,
            "bars": int(rule_opts.get("bars", 300)), "rule": rule.model_dump(),
        }
        self._save()
        return name, rule

    def remove(self, target: str) -> bool:
        for name, spec in list(self._active.items()):
            if name.lower() == target.lower() or spec.get("pine_id") == target or spec.get("display", "").lower() == target.lower():
                del self._active[name]
                self._save()
                return True
        return False

    def clear(self) -> None:
        self._active.clear()
        self._save()
