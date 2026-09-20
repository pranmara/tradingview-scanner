from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.asset_classifier import AssetInfo
from app.clients.binance import BinanceClient
from app.clients.bybit import BybitClient
from app.clients.tradingview_mcp import TradingViewMCPClient
from app.clients.tradingview_scanner import TradingViewScannerClient
from app.clients.tradingview_ws import TradingViewSessionClient
from app.clients.twelvedata import TwelveDataClient
from app.clients.yahoo import YahooClient
from app.config import Settings
from app.resilience import UpstreamError
from app.schemas import OHLCV, TechnicalSnapshot, Timeframe

logger = logging.getLogger(__name__)


class CompositeMarketDataProvider:
    """MCP first, then direct REST fallbacks (Binance then Bybit for crypto, Yahoo for stocks)."""

    def __init__(
        self,
        settings: Settings,
        binance: BinanceClient,
        yahoo: YahooClient,
        scanner: TradingViewScannerClient,
        mcp: TradingViewMCPClient | None = None,
        twelvedata: TwelveDataClient | None = None,
        tv_session: TradingViewSessionClient | None = None,
        bybit: BybitClient | None = None,
    ) -> None:
        self._settings = settings
        self._binance = binance
        self._bybit = bybit
        self._yahoo = yahoo
        self._scanner = scanner
        self._mcp = mcp
        self._twelvedata = twelvedata
        self._tv_session = tv_session

    def _tv_symbols(self, asset: AssetInfo) -> list[str]:
        return asset.tradingview_symbols(
            self._settings.stock_exchange_candidates, self._settings.tv_scanner_default_crypto_exchange
        )

    async def get_ohlcv(self, asset: AssetInfo, timeframe: Timeframe, limit: int) -> OHLCV:
        errors: list[str] = []
        if self._tv_session is not None:
            for ticker in self._tv_symbols(asset):
                try:
                    return await self._tv_session.get_ohlcv(ticker, timeframe, limit)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tv session ohlcv failed", extra={"symbol": ticker, "tf": timeframe.value, "error": str(exc)})
                    errors.append(f"tv-session {ticker}: {exc}")
        if self._mcp is not None:
            try:
                return await self._mcp.get_ohlcv(self._tv_symbols(asset)[0], timeframe, limit)
            except Exception as exc:  # noqa: BLE001 - any MCP failure must fall back
                logger.warning("mcp ohlcv failed, falling back", extra={"symbol": asset.symbol, "tf": timeframe.value, "error": str(exc)})
                errors.append(f"tv-mcp: {exc}")

        chain: list[tuple[str, Callable[[], Awaitable[OHLCV]]]] = []
        if asset.is_crypto:
            chain.append(("binance", lambda: self._binance.get_ohlcv(asset.pair_symbol, timeframe, limit)))
            if self._bybit is not None:
                # Plenty of tokens never list on Binance, or list on Bybit first.
                by = self._bybit
                chain.append(("bybit", lambda: by.get_ohlcv(asset.pair_symbol, timeframe, limit)))
        elif self._twelvedata is not None:
            td = self._twelvedata
            chain.append(("twelvedata", lambda: td.get_ohlcv(asset.yahoo_symbol, timeframe, limit)))
        chain.append(("yahoo", lambda: self._yahoo.get_ohlcv(asset.yahoo_symbol, timeframe, limit)))

        for name, fetch in chain:
            try:
                return await fetch()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ohlcv source failed", extra={"source": name, "symbol": asset.symbol, "tf": timeframe.value, "error": str(exc)})
                errors.append(f"{name}: {exc}")

        raise UpstreamError(f"all OHLCV sources failed for {asset.symbol} {timeframe.value}: " + " | ".join(errors))

    async def get_snapshot(self, asset: AssetInfo, timeframe: Timeframe) -> TechnicalSnapshot | None:
        tickers = self._tv_symbols(asset)
        if self._mcp is not None:
            try:
                snap = await self._mcp.get_snapshot(tickers[0], timeframe)
                if snap is not None:
                    return snap
            except Exception as exc:  # noqa: BLE001
                logger.warning("mcp snapshot failed, falling back", extra={"symbol": asset.symbol, "error": str(exc)})
        try:
            return await self._scanner.get_snapshot(tickers, timeframe, asset.is_crypto)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tv scanner snapshot failed", extra={"symbol": asset.symbol, "error": str(exc)})
            return None

    async def get_ohlcv_history(self, asset: AssetInfo, timeframe: Timeframe, bars: int) -> OHLCV:
        """Deep history for backtests. Binance and Bybit page; other sources return one request's worth."""
        if asset.is_crypto:
            try:
                return await self._binance.get_ohlcv_history(asset.pair_symbol, timeframe, bars)
            except Exception as exc:  # noqa: BLE001
                if self._bybit is None:
                    raise
                logger.warning("binance history failed, trying bybit",
                               extra={"symbol": asset.symbol, "tf": timeframe.value, "error": str(exc)})
                return await self._bybit.get_ohlcv_history(asset.pair_symbol, timeframe, bars)
        return await self.get_ohlcv(asset, timeframe, bars)

    async def get_benchmark_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        if self._tv_session is not None:
            for exchange in ("AMEX", *self._settings.stock_exchange_candidates):
                try:
                    return await self._tv_session.get_ohlcv(f"{exchange}:{symbol}", timeframe, limit)
                except Exception as exc:  # noqa: BLE001
                    logger.info("tv session benchmark attempt failed", extra={"symbol": f"{exchange}:{symbol}", "error": str(exc)})
        if self._twelvedata is not None:
            try:
                return await self._twelvedata.get_ohlcv(symbol, timeframe, limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("twelvedata benchmark failed, falling back to yahoo", extra={"symbol": symbol, "error": str(exc)})
        return await self._yahoo.get_ohlcv(symbol, timeframe, limit)

    async def health(self) -> dict[str, str]:
        async def mcp_status() -> str:
            if self._mcp is None:
                return "disabled"
            try:
                tools = await self._mcp.list_tools()
                return f"ok ({len(tools)} tools)"
            except Exception as exc:  # noqa: BLE001
                return f"down: {type(exc).__name__}"

        async def session_status() -> str:
            return "disabled" if self._tv_session is None else await self._tv_session.health()

        async def bybit_status() -> str:
            return "disabled" if self._bybit is None else ("ok" if await self._bybit.ping() else "down")

        mcp, binance, session, bybit = await asyncio.gather(
            mcp_status(), self._binance.ping(), session_status(), bybit_status()
        )
        return {
            "tradingview_session": session,
            "tradingview_mcp": mcp,
            "binance": "ok" if binance else "down",
            "bybit": bybit,
            "twelvedata": "configured" if self._twelvedata is not None else "disabled",
        }
