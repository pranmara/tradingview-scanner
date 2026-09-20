from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.alert_store import AlertStore
from app.asset_classifier import AssetInfo, classify
from app.asset_resolver import AssetResolver
from app.clients.market_data import CompositeMarketDataProvider
from app.clients.nansen import NansenClient
from app.clients.tradingview_ws import TradingViewSessionClient
from app.config import Settings
from app.custom_indicators import CustomIndicatorRules, IndicatorRule
from app.decision_engine import DecisionEngine, ScanInputs
from app.execution_router import ExecutionRouter
from app.indicator_matcher import IndicatorMatcher
from app.indicators import InsufficientDataError, analysis_from_snapshot, analyze_timeframe, candles_to_frame, return_pct
from app.logging_config import scan_id_var
from app.schemas import AlertSignal, ConfluenceReport, PineAlert, RelativeStrength, Signal, Timeframe, TimeframeAnalysis
from app.signal_journal import SignalJournal
from app.study_registry import StudyRegistry

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]

SECTOR_ETF: dict[str, str] = {
    "Technology Services": "XLK", "Electronic Technology": "XLK",
    "Finance": "XLF",
    "Health Technology": "XLV", "Health Services": "XLV",
    "Energy Minerals": "XLE", "Industrial Services": "XLE",
    "Consumer Non-Durables": "XLP", "Distribution Services": "XLP",
    "Retail Trade": "XLY", "Consumer Durables": "XLY", "Consumer Services": "XLY",
    "Utilities": "XLU",
    "Producer Manufacturing": "XLI", "Transportation": "XLI", "Commercial Services": "XLI",
    "Communications": "XLC",
    "Non-Energy Minerals": "XLB", "Process Industries": "XLB",
}


class ScanError(Exception):
    """User-facing scan failure."""


def scan_timeframes(primary: Timeframe) -> list[Timeframe]:
    """Primary first, then the confluence set; weekly is added for daily/weekly scans (HTF bias)."""
    tfs = [primary, Timeframe.H1, Timeframe.H4, Timeframe.D1]
    if primary in (Timeframe.D1, Timeframe.W1):
        tfs.append(Timeframe.W1)
    return list(dict.fromkeys(tfs))


class ScanOrchestrator:
    def __init__(
        self,
        settings: Settings,
        market: CompositeMarketDataProvider,
        nansen: NansenClient,
        alert_store: AlertStore,
        engine: DecisionEngine,
        executor: ExecutionRouter,
        journal: SignalJournal | None = None,
        tv_session: TradingViewSessionClient | None = None,
        studies: StudyRegistry | None = None,
        resolver: AssetResolver | None = None,
        rules: CustomIndicatorRules | None = None,
        matcher: IndicatorMatcher | None = None,
    ) -> None:
        self._s = settings
        self._market = market
        self._nansen = nansen
        self._alerts = alert_store
        self._engine = engine
        self._executor = executor
        self._journal = journal
        self._tv_session = tv_session
        self._studies = studies
        self._resolver = resolver
        self._rules = rules
        self._matcher = matcher

    async def scan(self, raw_symbol: str, primary: Timeframe, on_status: StatusCallback | None = None) -> ConfluenceReport:
        token = scan_id_var.set(uuid.uuid4().hex[:12])
        try:
            return await self._scan(raw_symbol, primary, on_status)
        finally:
            scan_id_var.reset(token)

    async def _scan(self, raw_symbol: str, primary: Timeframe, on_status: StatusCallback | None) -> ConfluenceReport:
        async def status(text: str) -> None:
            logger.info("scan status", extra={"status": text})
            if on_status is not None:
                await on_status(text)

        try:
            asset = classify(raw_symbol)
        except ValueError as exc:
            raise ScanError(str(exc)) from exc
        if asset.ambiguous and self._resolver is not None:
            # A bare ticker the static rules could not place. One cached lookup beats loading the wrong market.
            asset = await self._resolver.resolve(asset)

        timeframes = scan_timeframes(primary)
        logger.info("scan started", extra={"symbol": asset.symbol, "asset_class": asset.asset_class.value,
                                           "primary": primary.value, "timeframes": [t.value for t in timeframes]})

        await status("🔍 Fetching TradingView data...")
        results = await asyncio.gather(*(self._analyze_timeframe(asset, tf) for tf in timeframes), return_exceptions=True)

        analyses: dict[Timeframe, TimeframeAnalysis] = {}
        errors: list[str] = []
        sources: set[str] = set()
        for tf, res in zip(timeframes, results, strict=True):
            if isinstance(res, BaseException):
                errors.append(f"{tf.value}: {res}")
                logger.warning("timeframe analysis failed", extra={"symbol": asset.symbol, "tf": tf.value, "error": str(res)})
                continue
            analyses[tf] = res
            sources.add(res.source)
            if res.snapshot is not None:
                sources.add(res.snapshot.source)
            if res.degraded:
                errors.append(f"{tf.value}: candles unavailable — snapshot-only analysis (no structure/divergence/volume)")
        if primary not in analyses:
            raise ScanError(f"Could not load {primary.value} data for {asset.symbol}. " + "; ".join(errors))

        study_alerts: list[PineAlert] = []
        active_studies = self._studies.active() if self._studies is not None else {}
        if self._tv_session is not None and active_studies:
            await status(f"📈 Pulling {len(active_studies)} account indicator(s) from TradingView...")
            study_alerts = await self._fetch_studies(asset, primary, active_studies, errors)
            if study_alerts:
                sources.add("tv-session-studies")

        onchain = None
        rel_strength: RelativeStrength | None = None
        if asset.is_crypto:
            if self._s.nansen_active and self._nansen.enabled:
                await status("📊 Querying Nansen Smart Money (advisory)..." if self._s.nansen_mode == "advisory" else "📊 Querying Nansen Smart Money...")
                try:
                    onchain = await self._nansen.get_onchain_snapshot(asset)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"nansen: {exc}")
                    logger.warning("nansen fetch failed", extra={"symbol": asset.symbol, "error": str(exc)})
                if onchain is not None:
                    sources.add("nansen")
        else:
            await status("📊 Computing volume profile & sector relative strength...")
            try:
                rel_strength = await self._relative_strength(asset, primary, analyses[primary])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"benchmark: {exc}")
                logger.warning("relative strength failed", extra={"symbol": asset.symbol, "error": str(exc)})

        alerts = study_alerts + await self._alerts.recent(asset.symbol, [tf.value for tf in timeframes])
        extra_rules = self._studies.extra_rules() if self._studies is not None else {}
        extra_rules.update(await self._match_indicators(alerts, extra_rules, errors))

        await status("🧮 Scoring confluence matrix...")
        report = self._engine.evaluate(
            ScanInputs(
                asset=asset, primary=primary, analyses=analyses, onchain=onchain, relative_strength=rel_strength,
                alerts=alerts, data_sources=sorted(sources), errors=errors,
                extra_rules=extra_rules,
            )
        )
        logger.info("scan scored", extra={"symbol": asset.symbol, "signal": report.signal.value, "score": report.score,
                                          "bull": report.bullish_score, "bear": report.bearish_score, "coverage": report.coverage_pct})

        if self._journal is not None:
            self._journal.record(report)
        if report.signal in (Signal.BUY, Signal.SELL):
            outcome = await self._executor.dispatch(report)
            report.reasons.append(f"• Execution router: {outcome}")

        await status("✅ Final Decision Ready")
        return report

    async def _match_indicators(
        self, alerts: list[PineAlert], extra_rules: dict[str, IndicatorRule], errors: list[str]
    ) -> dict[str, IndicatorRule]:
        """A Pine alert whose name is not a config key scores on DEFAULT_RULE. Say so, and match it when we can."""
        if self._rules is None:
            return {}
        known = self._rules.merged(extra_rules)
        names = [a.indicator for a in alerts if a.indicator and not known.has(a.indicator)]
        if not names:
            return {}

        matched: dict[str, IndicatorRule] = {}
        unmatched = list(dict.fromkeys(names))
        if self._matcher is not None:
            try:
                matched, unmatched = await self._matcher.resolve(names, known)
            except Exception as exc:  # noqa: BLE001 - matching is an aid, never a scan failure
                logger.warning("indicator matching skipped", extra={"error": str(exc)})
        for name in unmatched:
            errors.append(f"pine alert '{name}' matches no rule in custom_indicators.json — scored with defaults")
        return matched

    async def _fetch_studies(self, asset: AssetInfo, tf: Timeframe, studies: dict[str, dict[str, Any]], errors: list[str]) -> list[PineAlert]:
        assert self._tv_session is not None
        ticker = asset.tradingview_symbols(self._s.stock_exchange_candidates, self._s.tv_scanner_default_crypto_exchange)[0]

        async def one(name: str, spec: dict[str, Any]) -> PineAlert | None:
            try:
                study = await self._tv_session.get_study(ticker, tf, str(spec["pine_id"]), spec.get("inputs") or {}, int(spec.get("bars", 300)))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"study {name}: {exc}")
                logger.warning("tv study failed", extra={"study": name, "symbol": ticker, "error": str(exc)})
                return None
            latest = {k: v for k, v in study.latest.items() if k != "ts"}
            if not latest:
                return None
            now_ms = int(time.time() * 1000)
            return PineAlert(ticker=asset.symbol, timeframe=tf.value, indicator=name, signal=AlertSignal.NEUTRAL,
                             price=0.0 if "close" not in latest else latest["close"], values=latest,
                             alert_ts_ms=int(study.latest.get("ts", now_ms)), received_at_ms=now_ms)

        results = await asyncio.gather(*(one(n, s) for n, s in studies.items()))
        return [r for r in results if r is not None]

    async def _analyze_timeframe(self, asset: AssetInfo, tf: Timeframe) -> TimeframeAnalysis:
        ohlcv, snapshot = await asyncio.gather(
            self._market.get_ohlcv(asset, tf, self._s.candle_limit),
            self._market.get_snapshot(asset, tf),
            return_exceptions=True,
        )
        if isinstance(snapshot, BaseException):
            logger.warning("snapshot fetch failed", extra={"symbol": asset.symbol, "tf": tf.value, "error": str(snapshot)})
            snapshot = None
        if isinstance(ohlcv, BaseException):
            if snapshot is None:
                raise ohlcv
            logger.warning("candles unavailable; degrading to snapshot-only analysis",
                           extra={"symbol": asset.symbol, "tf": tf.value, "error": str(ohlcv)})
            try:
                return analysis_from_snapshot(snapshot)
            except InsufficientDataError:
                raise ohlcv from None
        try:
            analysis = analyze_timeframe(candles_to_frame(ohlcv.candles), tf, ohlcv.source)
        except InsufficientDataError as exc:
            raise ScanError(str(exc)) from exc
        analysis.snapshot = snapshot
        return analysis

    async def _relative_strength(self, asset: AssetInfo, tf: Timeframe, primary: TimeframeAnalysis) -> RelativeStrength | None:
        if primary.return_20_pct is None:
            return None
        sector = primary.snapshot.sector if primary.snapshot is not None else None
        benchmark = SECTOR_ETF.get(sector or "", self._s.benchmark_symbol)
        if benchmark.upper() == asset.symbol:
            return None
        ohlcv = await self._market.get_benchmark_ohlcv(benchmark, tf, self._s.candle_limit)
        bench_ret = return_pct(candles_to_frame(ohlcv.candles)["close"])
        if bench_ret is None:
            return None
        return RelativeStrength(benchmark=benchmark, asset_return_pct=primary.return_20_pct, benchmark_return_pct=bench_ret)
