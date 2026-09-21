from __future__ import annotations

import logging
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.alert_store import AlertStore
from app.asset_resolver import build_resolver
from app.clients.binance import BinanceClient
from app.clients.bybit import BybitClient
from app.clients.market_data import CompositeMarketDataProvider, public_market_provider
from app.clients.nansen import NansenClient
from app.clients.tradingview_mcp import TradingViewMCPClient
from app.clients.tradingview_scanner import TradingViewScannerClient
from app.clients.tradingview_ws import TradingViewSessionClient
from app.clients.twelvedata import TwelveDataClient
from app.clients.typesafe import aclose as close_typesafe
from app.clients.typesafe import build_client as build_typesafe
from app.clients.yahoo import YahooClient
from app.command_router import build_router
from app.config import get_settings
from app.custom_indicators import CustomIndicatorRules
from app.decision_engine import DecisionEngine
from app.execution_router import ExecutionRouter
from app.indicator_matcher import build_matcher
from app.journal_runner import JournalRunner
from app.logging_config import setup_logging
from app.orchestrator import ScanOrchestrator
from app.signal_journal import SignalJournal
from app.study_advisor import build_advisor
from app.study_registry import StudyRegistry
from app.telegram_bot import build_application
from app.tradingview_webhook import router as webhook_router

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds, connect=5.0),
        headers={"User-Agent": f"{settings.app_name}/1.0"},
        follow_redirects=True,
    )

    redis: Redis | None = None
    if settings.redis_url:
        redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3.0)
        try:
            await redis.ping()
        except Exception as exc:  # noqa: BLE001
            logger.error("redis unreachable, using in-memory alert store", extra={"error": str(exc)})
            await redis.aclose()
            redis = None

    mcp = (
        TradingViewMCPClient(settings.tv_mcp_url, settings.tv_mcp_ohlcv_tool, settings.tv_mcp_snapshot_tool, settings.tv_mcp_timeout_seconds)
        if settings.tv_mcp_url
        else None
    )
    tv_session = (
        TradingViewSessionClient(
            http,
            session_id=settings.tv_session_id.get_secret_value() if settings.tv_session_id else None,
            session_sign=settings.tv_session_id_sign.get_secret_value() if settings.tv_session_id_sign else None,
            username=settings.tv_username,
            password=settings.tv_password.get_secret_value() if settings.tv_password else None,
            timeout_seconds=settings.tv_session_timeout_seconds,
        )
        if settings.tv_feed_enabled
        else None
    )
    market = CompositeMarketDataProvider(
        settings,
        binance=BinanceClient(http),
        bybit=BybitClient(http, settings.bybit_base_url) if settings.bybit_enabled else None,
        yahoo=YahooClient(http),
        scanner=TradingViewScannerClient(http, settings.tv_scanner_stock_market),
        mcp=mcp,
        twelvedata=TwelveDataClient(http, settings.twelvedata_api_key.get_secret_value()) if settings.twelvedata_api_key else None,
        tv_session=tv_session,
    )
    nansen = NansenClient(
        http,
        api_key=settings.nansen_api_key.get_secret_value() if settings.nansen_api_key else None,
        base_url=settings.nansen_base_url,
        token_map=NansenClient.load_token_map(settings.nansen_token_map_path),
        cache=redis,
        cache_ttl_seconds=settings.nansen_cache_ttl_seconds,
    )
    alert_store = AlertStore(redis, settings.tv_alert_ttl_seconds)
    rules = CustomIndicatorRules.load(settings.custom_indicators_path)
    studies = StudyRegistry(settings.tv_studies_active_path, settings.tv_studies_path)
    typesafe = build_typesafe(settings)
    advisor = build_advisor(settings, typesafe)
    nl_router = build_router(settings, typesafe)
    resolver = build_resolver(settings, typesafe, cache=redis)
    matcher = build_matcher(settings, typesafe, cache=redis)
    orchestrator = ScanOrchestrator(
        settings, market, nansen, alert_store, DecisionEngine(settings, rules), ExecutionRouter(settings, http),
        journal=SignalJournal(settings.signal_journal_path), tv_session=tv_session, studies=studies,
        resolver=resolver, rules=rules, matcher=matcher,
    )

    # The forward journal gets its own orchestrator. Its settings copy disables execution and Nansen, and its
    # market provider uses public endpoints only — so a scheduled scan cannot send an order, spend credits, or
    # load the user's TradingView session. No TypeSafe, no account studies: it measures the base score.
    runner: JournalRunner | None = None
    if settings.journal_enabled:
        jset = settings.model_copy(update={"execution_enabled": False, "nansen_mode": "off"})
        jmarket = public_market_provider(http, jset)
        runner = JournalRunner(
            jset,
            ScanOrchestrator(jset, jmarket, nansen, alert_store, DecisionEngine(jset, rules), ExecutionRouter(jset, http),
                             journal=SignalJournal(settings.signal_journal_path, source="scheduled")),
            jmarket, settings.signal_journal_path,
        )

    app.state.settings = settings
    app.state.alert_store = alert_store
    app.state.orchestrator = orchestrator

    telegram = build_application(settings, orchestrator, market, alert_store, nansen, tv_session=tv_session, studies=studies,
                                 advisor=advisor, router=nl_router, journal=runner)
    await telegram.initialize()
    await telegram.start()
    assert telegram.updater is not None
    await telegram.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])

    journal_task: asyncio.Task[None] | None = None
    if runner is not None:
        async def notify(text: str) -> None:
            for uid in settings.allowed_user_ids:
                try:
                    await telegram.bot.send_message(uid, text, parse_mode="HTML")
                except Exception as exc:  # noqa: BLE001 - one unreachable user must not block the others
                    logger.warning("journal digest not delivered", extra={"user_id": uid, "error": str(exc)})

        runner.notify = notify
        journal_task = asyncio.create_task(runner.run_forever(), name="journal-runner")
    logger.info(
        "service started",
        extra={
            "mcp": bool(mcp), "redis": redis is not None, "nansen": f"{settings.nansen_mode}/{'key' if nansen.enabled else 'no-key'}",
            "tv_session": tv_session is not None, "tv_studies": sorted(studies.active()),
            "journal": f"{len(settings.journal_symbols)} symbols/{settings.journal_timeframe}" if runner else "off",
            "typesafe": f"autoconfig={advisor is not None} natural_language={nl_router is not None} symbols={resolver is not None} indicators={matcher is not None}",
            "execution": "live" if settings.execution_live else ("dry-run" if settings.execution_enabled else "disabled"),
            "allowed_users": len(settings.allowed_user_ids), "custom_indicator_rules": rules.names,
        },
    )
    try:
        yield
    finally:
        logger.info("service stopping")
        if journal_task is not None:
            journal_task.cancel()
            try:
                await journal_task
            except asyncio.CancelledError:
                pass
        if telegram.updater is not None and telegram.updater.running:
            await telegram.updater.stop()
        if telegram.running:
            await telegram.stop()
        await telegram.shutdown()
        await close_typesafe(typesafe)
        if redis is not None:
            await redis.aclose()
        await http.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(webhook_router)
