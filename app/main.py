from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.alert_store import AlertStore
from app.clients.binance import BinanceClient
from app.clients.market_data import CompositeMarketDataProvider
from app.clients.nansen import NansenClient
from app.clients.tradingview_mcp import TradingViewMCPClient
from app.clients.tradingview_scanner import TradingViewScannerClient
from app.clients.tradingview_ws import TradingViewSessionClient
from app.clients.twelvedata import TwelveDataClient
from app.clients.yahoo import YahooClient
from app.config import get_settings
from app.custom_indicators import CustomIndicatorRules
from app.decision_engine import DecisionEngine
from app.execution_router import ExecutionRouter
from app.logging_config import setup_logging
from app.orchestrator import ScanOrchestrator
from app.signal_journal import SignalJournal
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
    orchestrator = ScanOrchestrator(
        settings, market, nansen, alert_store, DecisionEngine(settings, rules), ExecutionRouter(settings, http),
        journal=SignalJournal(settings.signal_journal_path), tv_session=tv_session, studies=studies,
    )

    app.state.settings = settings
    app.state.alert_store = alert_store
    app.state.orchestrator = orchestrator

    telegram = build_application(settings, orchestrator, market, alert_store, nansen, tv_session=tv_session, studies=studies)
    await telegram.initialize()
    await telegram.start()
    assert telegram.updater is not None
    await telegram.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])
    logger.info(
        "service started",
        extra={
            "mcp": bool(mcp), "redis": redis is not None, "nansen": f"{settings.nansen_mode}/{'key' if nansen.enabled else 'no-key'}",
            "tv_session": tv_session is not None, "tv_studies": sorted(studies.active()),
            "execution": "live" if settings.execution_live else ("dry-run" if settings.execution_enabled else "disabled"),
            "allowed_users": len(settings.allowed_user_ids), "custom_indicator_rules": rules.names,
        },
    )
    try:
        yield
    finally:
        logger.info("service stopping")
        if telegram.updater is not None and telegram.updater.running:
            await telegram.updater.stop()
        if telegram.running:
            await telegram.stop()
        await telegram.shutdown()
        if redis is not None:
            await redis.aclose()
        await http.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(webhook_router)
