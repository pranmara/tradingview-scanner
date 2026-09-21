from __future__ import annotations

import asyncio
import gzip
import html
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.asset_classifier import classify
from app.backtest import _first_hit
from app.config import Settings
from app.indicators import candles_to_frame
from app.schemas import Side, Timeframe
from app.signal_journal import read_journal
from app.timeframes import BAR_MS, parse_timeframe

logger = logging.getLogger(__name__)

# The forward test the backtests cannot give: every report is recorded before its outcome exists, then scored
# once the bars arrive. Calibration uses one observation per symbol per ISO week, because the outcome horizon
# (40 bars of 4h, ~6.7 days) would otherwise overlap consecutive scans and make the interval look far tighter
# than the evidence is.

MIN_RESOLVED_TO_JUDGE = 50
MIN_NAMES_PER_WEEK = 10    # a within-week ranking of fewer names than this is too noisy to count
MIN_WEEKS_TO_JUDGE = 8     # weeks are the independent unit, so the verdict waits on weeks, not rows
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024   # bots may upload 50 MB; keep a margin
HEALTHY_RATIO = 0.90

Notify = Callable[[str], Awaitable[None]]
Backup = Callable[[bytes, str], Awaitable[None]]


# ---------------------------------------------------------------------------- schedule
def next_slot_ms(now_ms: int, interval_hours: int, offset_minutes: int) -> int:
    """Next slot on a grid anchored to 00:00 UTC, `offset_minutes` after each bar closes."""
    period = interval_hours * HOUR_MS
    slot = (now_ms // period) * period + offset_minutes * 60_000
    return slot if slot > now_ms else slot + period


def is_digest_slot(slot_ms: int, weekday: int, hour_utc: int, interval_hours: int) -> bool:
    """True for exactly one slot per week: the one whose window holds the digest hour on the digest weekday."""
    moment = datetime.fromtimestamp(slot_ms / 1000, UTC)
    window_start = (hour_utc // interval_hours) * interval_hours
    return moment.weekday() == weekday and moment.hour == window_start


# ---------------------------------------------------------------------------- statistics
def _rank(a: np.ndarray) -> np.ndarray:
    order = a.argsort(kind="mergesort")
    r = np.empty(len(a))
    r[order] = np.arange(1, len(a) + 1)
    _, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    return (np.bincount(inv, weights=r) / cnt)[inv]   # average ranks within ties


def auc_with_ci(scores: Iterable[float], hits: Iterable[bool]) -> tuple[float, float, float] | None:
    """Mann-Whitney AUC with a Hanley-McNeil 95% interval. None when one class is empty."""
    x, y = np.asarray(list(scores), float), np.asarray(list(hits), bool)
    pos, neg = int(y.sum()), int((~y).sum())
    if pos == 0 or neg == 0:
        return None
    a = float((_rank(x)[y].sum() - pos * (pos + 1) / 2) / (pos * neg))
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    se = math.sqrt(max(0.0, (a * (1 - a) + (pos - 1) * (q1 - a * a) + (neg - 1) * (q2 - a * a)) / (pos * neg)))
    return a, max(0.0, a - 1.96 * se), min(1.0, a + 1.96 * se)


# ---------------------------------------------------------------------------- outcomes
def resolve(rec: dict[str, Any], df: pd.DataFrame, time_stop_bars: int) -> tuple[str, float | None]:
    """('pending'|'tp2'|'sl'|'time', r_multiple). A record whose time stop has not elapsed yet stays pending —
    it must not be scored at today's close as though the trade had run its course."""
    start = int(np.searchsorted(df["ts"].to_numpy(), rec["ts_ms"], side="right"))
    if start >= len(df):
        return "pending", None
    side = Side(rec["direction"])
    j, reason, px = _first_hit(df, start, side, rec["stop_loss"], [rec["tp2"]], time_stop_bars)
    if reason == "time" and start + time_stop_bars > len(df):
        return "pending", None
    risk = abs(rec["entry"] - rec["stop_loss"])
    if risk <= 0:
        return "pending", None
    r = (px - rec["entry"]) / risk if side is Side.BUY else (rec["entry"] - px) / risk
    return ("tp2" if reason == "tp1" else reason), round(float(r), 3)


def weekly_sample(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """First scheduled record per symbol, timeframe and ISO week: independent at a ~7-day outcome horizon."""
    seen: set[tuple[str, str, int, int]] = set()
    out: list[dict[str, Any]] = []
    for rec in sorted(records, key=lambda r: r["ts_ms"]):
        iso = datetime.fromtimestamp(rec["ts_ms"] / 1000, UTC).isocalendar()
        key = (rec["symbol"], rec["timeframe"], iso.year, iso.week)
        if key not in seen:
            seen.add(key)
            out.append(rec)
    return out


@dataclass
class ForwardReport:
    first_ts: int | None = None
    total: int = 0
    by_source: Counter = field(default_factory=Counter)
    by_signal: Counter = field(default_factory=Counter)
    independent: int = 0
    resolved: int = 0
    pending: int = 0
    base_rate: float | None = None
    auc: tuple[float, float, float] | None = None
    trades: list[float] = field(default_factory=list)
    trades_pending: int = 0
    ic_weeks: list[float] = field(default_factory=list)


def forward_report(records: list[dict[str, Any]], candles_for: dict[tuple[str, str], pd.DataFrame],
                   time_stop_bars: int) -> ForwardReport:
    rep = ForwardReport(total=len(records))
    if not records:
        return rep
    rep.first_ts = min(r["ts_ms"] for r in records)
    rep.by_source = Counter(r.get("source", "manual") for r in records)
    rep.by_signal = Counter(r.get("signal") for r in records)

    usable = [r for r in records if r.get("source") == "scheduled" and r.get("direction")
              and r.get("entry") and r.get("stop_loss") and r.get("tp2")]
    sample = weekly_sample(usable)
    rep.independent = len(sample)
    scores, hits = [], []
    by_week: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for rec in sample:
        df = candles_for.get((rec["symbol"], rec["timeframe"]))
        if df is None:
            rep.pending += 1
            continue
        outcome, r = resolve(rec, df, time_stop_bars)
        if outcome == "pending" or r is None:
            rep.pending += 1
            continue
        scores.append(rec["score"])
        hits.append(outcome == "tp2")
        iso = datetime.fromtimestamp(rec["ts_ms"] / 1000, UTC).isocalendar()
        by_week.setdefault((iso.year, iso.week), []).append((rec["score"], r))
    rep.resolved = len(hits)
    if hits:
        rep.base_rate = sum(hits) / len(hits)
        rep.auc = auc_with_ci(scores, hits)

    # Names in the same week share the market's move, so they are not independent observations. Ranking them
    # against each other within the week cancels that shared move, and each week then counts once.
    for _, pairs in sorted(by_week.items()):
        if len(pairs) < MIN_NAMES_PER_WEEK:
            continue
        x, y = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        rx, ry = _rank(x), _rank(y)
        if rx.std() > 0 and ry.std() > 0:
            rep.ic_weeks.append(float(np.corrcoef(rx, ry)[0, 1]))

    for rec in usable:   # actual BUY/SELL calls, every one of them — they are rare enough not to overlap much
        if rec.get("signal") not in ("BUY", "SELL"):
            continue
        df = candles_for.get((rec["symbol"], rec["timeframe"]))
        outcome, r = resolve(rec, df, time_stop_bars) if df is not None else ("pending", None)
        if outcome == "pending" or r is None:
            rep.trades_pending += 1
        else:
            rep.trades.append(r)
    return rep


def format_digest(rep: ForwardReport, settings: Settings, health: Health | None = None) -> str:
    e = html.escape
    if rep.total == 0:
        return "📓 <b>Forward journal</b>\nNothing recorded yet. The first scheduled scan runs after the next candle closes."
    since = datetime.fromtimestamp(rep.first_ts / 1000, UTC).strftime("%Y-%m-%d") if rep.first_ts else "?"
    sources = ", ".join(f"{n} {e(str(k))}" for k, n in rep.by_source.most_common())
    lines = [
        "📓 <b>Forward journal</b>",
        f"Since {since}: {rep.total} reports ({sources})",
        f"Watchlist: {len(settings.journal_symbols)} symbols · {e(settings.journal_timeframe)} · "
        f"every {settings.journal_interval_hours}h",
    ]
    if health is not None:
        runs = "run" if health.empty_slots == 1 else "runs"
        lines.append(f"Scans this week: {health.actual:,} of {health.expected:,} expected "
                     f"({100 * health.ratio:.0f}%) · {health.empty_slots} missed {runs}")
        if health.empty_slots:
            lines.append(f"⚠️ {health.empty_slots} scheduled {runs} wrote nothing — the runner or the VPS was down. "
                         f"Check <code>docker compose logs app</code>.")
        elif health.ratio < HEALTHY_RATIO:
            lines.append("⚠️ Many scans failed — a data source may have changed. "
                         "Check <code>docker compose logs app</code>.")
    lines += [
        "",
        "<b>Does the score rank this week's names against each other?</b>",
        "<i>Spearman IC of score vs realised R within each week · each week counts once</i>",
    ]
    weeks = np.array(rep.ic_weeks)
    if len(weeks) < MIN_WEEKS_TO_JUDGE:
        lines.append(f"⏳ {len(weeks)} of {MIN_WEEKS_TO_JUDGE} resolved weeks with ≥{MIN_NAMES_PER_WEEK} names. "
                     f"Keep collecting.")
    else:
        t_ic = weeks.mean() / (weeks.std(ddof=1) / math.sqrt(len(weeks))) if weeks.std() > 0 else float("nan")
        verdict = ("the score ranks outcomes" if t_ic > 2 else
                   "the score ranks outcomes backwards" if t_ic < -2 else
                   "no measurable ranking")
        lines.append(f"Mean IC <b>{weeks.mean():+.3f}</b> · t = {t_ic:.2f} over {len(weeks)} weeks · "
                     f"{100 * (weeks > 0).mean():.0f}% of weeks positive → {verdict}")
    lines += [
        "",
        "<b>Pooled hit rate</b>",
        f"<i>TP2 before SL within {settings.backtest_time_stop_bars} bars · one observation per symbol per week. "
        f"Its interval assumes names are independent, which same-week crypto is not — read it as optimistic.</i>",
        f"Observations: {rep.independent} ({rep.resolved} resolved, {rep.pending} pending)",
    ]
    if rep.resolved < MIN_RESOLVED_TO_JUDGE:
        lines.append(f"⏳ Too few resolved to judge — need {MIN_RESOLVED_TO_JUDGE}, have {rep.resolved}. Keep collecting.")
    elif rep.auc is None:
        lines.append("Every resolved observation had the same outcome, so ranking can't be measured yet.")
    else:
        a, lo, hi = rep.auc
        verdict = ("ranks better than chance" if lo > 0.5 else
                   "ranks worse than chance" if hi < 0.5 else
                   "not distinguishable from a coin flip")
        lines.append(f"AUC <b>{a:.3f}</b> (95% CI {lo:.3f}–{hi:.3f}) → {verdict}")
    if rep.base_rate is not None:
        lines.append(f"Base rate: {100 * rep.base_rate:.1f}% of resolved observations reached TP2")

    lines += ["", "<b>BUY/SELL calls</b>"]
    if not rep.trades and not rep.trades_pending:
        lines.append("None yet — a score of 80 fires rarely, which is why the ranking above is the real test.")
    else:
        if rep.trades:
            rs = np.array(rep.trades)
            lines.append(f"{len(rs)} resolved · avg {rs.mean():+.2f}R · win {100 * (rs > 0).mean():.0f}% · "
                         f"total {rs.sum():+.2f}R")
        if rep.trades_pending:
            lines.append(f"{rep.trades_pending} still open")
    lines += ["", "<i>Backtests said AUC 0.48 and −0.18R per trade (docs/CALIBRATION.md). "
                  "This is the out-of-sample check. Research only, not advice.</i>"]
    return "\n".join(lines)



# ---------------------------------------------------------------------------- health
@dataclass
class Health:
    slots: int          # scheduled runs that should have happened in the window
    empty_slots: int    # runs that produced nothing at all: the runner or the VPS was down
    expected: int       # slots x symbols
    actual: int         # reports actually written

    @property
    def ratio(self) -> float:
        return self.actual / self.expected if self.expected else 0.0


def journal_health(records: list[dict[str, Any]], now_ms: int, n_symbols: int, interval_hours: int,
                   offset_minutes: int, window_days: int = 7, settle_minutes: int = 15) -> Health | None:
    """Did the runner actually run? Counts scheduled reports per slot over the last week, or since the first
    scheduled report if that is more recent, so a dead runner shows up within a week rather than as a digest
    that is quietly thin. The newest slot is skipped while its ~1-minute scan may still be in progress."""
    stamps = sorted(r["ts_ms"] for r in records if r.get("source") == "scheduled")
    if not stamps or n_symbols <= 0:
        return None
    period, offset = interval_hours * HOUR_MS, offset_minutes * 60_000

    def slot_of(ts: int) -> int:
        return ((ts - offset) // period) * period + offset

    window_slot = slot_of(now_ms - window_days * DAY_MS)
    if window_slot < now_ms - window_days * DAY_MS:
        window_slot += period
    first = max(slot_of(stamps[0]), window_slot)
    last = slot_of(now_ms - settle_minutes * 60_000)
    if last < first:
        return None
    slots = list(range(first, last + 1, period))
    per_slot = Counter(slot_of(ts) for ts in stamps if first <= ts < last + period)
    return Health(slots=len(slots), empty_slots=sum(1 for sl in slots if per_slot.get(sl, 0) == 0),
                  expected=len(slots) * n_symbols, actual=sum(per_slot.get(sl, 0) for sl in slots))


def backup_payload(path: str | Path, now_ms: int | None = None) -> tuple[bytes, str] | None:
    """The journal, gzipped and named by date. Forward data cannot be re-downloaded if the VPS dies, so the
    weekly digest carries a copy off the box. JSONL compresses about 10x, far under Telegram's upload limit."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    day = datetime.fromtimestamp((now_ms or int(time.time() * 1000)) / 1000, UTC).strftime("%Y-%m-%d")
    return gzip.compress(p.read_bytes(), compresslevel=9), f"signals-{day}.jsonl.gz"


# ---------------------------------------------------------------------------- runner
@dataclass
class ScanSummary:
    scanned: int = 0
    failed: list[str] = field(default_factory=list)
    signals: Counter = field(default_factory=Counter)


class JournalRunner:
    """Scans a fixed watchlist on a fixed calendar and posts a weekly digest.

    Built on its own orchestrator whose settings disable execution and Nansen and whose market provider uses
    public endpoints only — so a scheduled scan cannot place an order, spend credits, or touch the user's
    TradingView account."""

    def __init__(self, settings: Settings, orchestrator: Any, market: Any, journal_path: str,
                 notify: Notify | None = None, pause_seconds: float = 1.0, backup: Backup | None = None) -> None:
        self._s = settings
        self._orch = orchestrator
        self._market = market
        self._path = journal_path
        self.notify = notify
        self.backup = backup
        self._pause = pause_seconds
        tf = parse_timeframe(settings.journal_timeframe)
        if tf is None:
            raise ValueError(f"JOURNAL_TIMEFRAME {settings.journal_timeframe!r} is not a supported timeframe")
        self._tf: Timeframe = tf

    async def scan_watchlist(self) -> ScanSummary:
        summary = ScanSummary()
        for sym in self._s.journal_symbols:
            try:
                report = await self._orch.scan(sym, self._tf)
                summary.scanned += 1
                summary.signals[report.signal.value] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest of the watchlist
                summary.failed.append(sym)
                logger.warning("journal scan failed", extra={"symbol": sym, "error": str(exc)})
            await asyncio.sleep(self._pause)
        logger.info("journal watchlist scanned", extra={"scanned": summary.scanned, "failed": summary.failed,
                                                         "signals": dict(summary.signals)})
        return summary

    async def _candles(self, records: list[dict[str, Any]]) -> dict[tuple[str, str], pd.DataFrame]:
        out: dict[tuple[str, str], pd.DataFrame] = {}
        now = int(time.time() * 1000)
        for sym, tf_label in {(r["symbol"], r["timeframe"]) for r in records if r.get("direction")}:
            tf = parse_timeframe(tf_label)
            if tf is None:
                continue
            first = min(r["ts_ms"] for r in records if r["symbol"] == sym and r["timeframe"] == tf_label)
            bars = min(5000, (now - first) // BAR_MS[tf] + self._s.backtest_time_stop_bars + 50)
            try:
                ohlcv = await self._market.get_ohlcv_history(classify(sym), tf, int(bars))
                out[(sym, tf_label)] = candles_to_frame(ohlcv.candles)
            except Exception as exc:  # noqa: BLE001 - that symbol's records just stay pending
                logger.warning("journal candles unavailable", extra={"symbol": sym, "error": str(exc)})
        return out

    async def build_digest(self) -> str:
        records = read_journal(self._path)
        candles = await self._candles(records)
        s = self._s
        health = journal_health(records, int(time.time() * 1000), len(s.journal_symbols),
                                s.journal_interval_hours, s.journal_offset_minutes)
        return format_digest(forward_report(records, candles, s.backtest_time_stop_bars), s, health)

    def backup_payload(self) -> tuple[bytes, str] | None:
        return backup_payload(self._path)

    async def send_backup(self) -> str:
        """Sends the gzipped journal through the backup callback, and says what happened."""
        if self.backup is None:
            return "no backup destination configured"
        payload = self.backup_payload()
        if payload is None:
            return "journal is empty, nothing to back up"
        data, name = payload
        if len(data) > TELEGRAM_UPLOAD_LIMIT:
            logger.warning("journal backup too large for telegram", extra={"bytes": len(data)})
            return (f"backup is {len(data) / 1e6:.0f} MB, over Telegram's limit — "
                    f"copy data/signals.jsonl off the VPS by hand")
        await self.backup(data, name)
        return f"sent {name} ({len(data) / 1024:.0f} KB)"

    async def run_forever(self) -> None:
        s = self._s
        logger.info("journal runner started", extra={"symbols": list(s.journal_symbols), "tf": self._tf.value,
                                                     "interval_h": s.journal_interval_hours})
        while True:
            now = int(time.time() * 1000)
            slot = next_slot_ms(now, s.journal_interval_hours, s.journal_offset_minutes)
            await asyncio.sleep((slot - now) / 1000)
            try:
                await self.scan_watchlist()
                if self.notify is not None and is_digest_slot(slot, s.journal_digest_weekday,
                                                               s.journal_digest_hour_utc, s.journal_interval_hours):
                    await self.notify(await self.build_digest())
                    logger.info("journal backup", extra={"result": await self.send_backup()})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad cycle is logged and the next slot still runs
                logger.exception("journal cycle failed")


__all__ = ["Health", "backup_payload", "journal_health", "MIN_NAMES_PER_WEEK", "MIN_WEEKS_TO_JUDGE", "JournalRunner", "ForwardReport", "auc_with_ci", "format_digest", "forward_report",
           "is_digest_slot", "next_slot_ms", "resolve", "weekly_sample"]
