from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.schemas import ConfluenceReport, Signal

logger = logging.getLogger(__name__)


class SignalJournal:
    """Append-only JSONL of live BUY/SELL/WATCH reports for later forward-testing (`python -m app.backtest --journal`)."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def record(self, report: ConfluenceReport) -> None:
        if report.signal is Signal.NEUTRAL:
            return
        lv = report.levels
        row = {
            "ts_ms": int(time.time() * 1000),
            "symbol": report.symbol,
            "timeframe": report.primary_timeframe.value,
            "signal": report.signal.value,
            "direction": report.direction.value if report.direction else None,
            "score": report.score,
            "coverage": report.coverage_pct,
            "entry": lv.entry if lv else None,
            "stop_loss": lv.stop_loss if lv else None,
            "tp1": lv.tp1 if lv else None,
            "tp2": lv.tp2 if lv else None,
            "tp3": lv.tp3 if lv else None,
            "vetoes": report.vetoes,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as exc:
            logger.warning("signal journal write failed", extra={"path": str(self._path), "error": str(exc)})
