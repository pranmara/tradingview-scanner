from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.schemas import ConfluenceReport

logger = logging.getLogger(__name__)


class SignalJournal:
    """Append-only JSONL of live reports, scored later against candles that had not happened yet.

    Every report is recorded, NEUTRAL included. The question the journal exists to answer is whether the score
    ranks outcomes, and that needs the whole distribution — recording only BUY/SELL would collect a handful of
    rows a year, since a score of 80 fires roughly once per 4,000 bars.
    """

    def __init__(self, path: str, source: str = "manual") -> None:
        self._path = Path(path)
        self._source = source  # "manual" for /scan, "scheduled" for the watchlist runner

    @property
    def path(self) -> Path:
        return self._path

    def record(self, report: ConfluenceReport) -> None:
        lv = report.levels
        row = {
            "ts_ms": int(time.time() * 1000),
            "source": self._source,
            "symbol": report.symbol,
            "timeframe": report.primary_timeframe.value,
            "signal": report.signal.value,
            "direction": report.direction.value if report.direction else None,
            "score": report.score,
            "bullish_score": report.bullish_score,
            "bearish_score": report.bearish_score,
            "coverage": report.coverage_pct,
            "entry": lv.entry if lv else None,
            "stop_loss": lv.stop_loss if lv else None,
            "tp1": lv.tp1 if lv else None,
            "tp2": lv.tp2 if lv else None,
            "tp3": lv.tp3 if lv else None,
            "effective_rrr": lv.effective_rrr if lv else None,
            "vetoes": report.vetoes,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as exc:
            logger.warning("signal journal write failed", extra={"path": str(self._path), "error": str(exc)})


def read_journal(path: str | Path) -> list[dict[str, Any]]:
    """Every parseable row. A torn final line — the process died mid-write — is skipped, not fatal."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping unreadable journal line", extra={"path": str(p)})
    return rows


__all__ = ["SignalJournal", "read_journal"]
