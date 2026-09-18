from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Published TradingView alert webhook egress IPs.
TRADINGVIEW_WEBHOOK_IPS: tuple[str, ...] = (
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "tradingview-scanner"
    log_level: str = "INFO"
    http_timeout_seconds: float = 15.0

    # Telegram
    telegram_bot_token: SecretStr
    telegram_allowed_user_ids: str = ""
    telegram_scan_cooldown_seconds: int = 10

    # TradingView webhook ingestion
    tv_webhook_secret: SecretStr
    tv_webhook_hmac_key: SecretStr | None = None
    tv_webhook_enforce_ip_allowlist: bool = True
    tv_webhook_ip_allowlist: str = ",".join(TRADINGVIEW_WEBHOOK_IPS)
    tv_webhook_trust_proxy: bool = False
    tv_webhook_max_skew_seconds: int = 300
    tv_alert_ttl_seconds: int = 6 * 3600

    # TradingView chart feed (unofficial websocket). Works anonymously for candles; a sessionid cookie unlocks
    # your plan's data and account-only indicators.
    tv_feed_enabled: bool = True
    tv_session_id: SecretStr | None = None
    tv_session_id_sign: SecretStr | None = None
    tv_username: str | None = None
    tv_password: SecretStr | None = None
    tv_session_timeout_seconds: float = 25.0
    tv_studies_path: str = "config/tv_studies.json"
    tv_studies_active_path: str = "data/tv_studies_active.json"

    # TradingView MCP
    tv_mcp_url: str | None = None
    tv_mcp_ohlcv_tool: str = "get_ohlcv"
    tv_mcp_snapshot_tool: str = "get_technical_analysis"
    tv_mcp_timeout_seconds: float = 20.0

    # TradingView scanner REST fallback
    tv_scanner_stock_market: str = "america"
    tv_scanner_default_stock_exchanges: str = "NASDAQ,NYSE,AMEX"
    tv_scanner_default_crypto_exchange: str = "BINANCE"

    # Optional keyed stock candle source (used before Yahoo when set)
    twelvedata_api_key: SecretStr | None = None

    # Nansen — off: never queried; advisory: points + cautions, never blocks; strict: points + hard vetoes
    nansen_mode: Literal["off", "advisory", "strict"] = "advisory"
    nansen_api_key: SecretStr | None = None
    nansen_base_url: str = "https://api.nansen.ai/api/v1"
    nansen_cache_ttl_seconds: int = 300
    nansen_token_map_path: str = "config/nansen_token_map.json"
    nansen_sm_netflow_full_score_usd: float = 1_000_000.0
    nansen_exchange_inflow_veto_usd: float = 5_000_000.0

    # Storage
    redis_url: str | None = None

    # Execution
    execution_enabled: bool = False
    dry_run: bool = True
    execution_webhook_url: str | None = None
    execution_hmac_key: SecretStr | None = None

    # Decision engine
    min_signal_score: float = 80.0
    watch_score: float = 60.0
    min_rrr: float = 2.5
    atr_multiplier: float = 1.5
    max_stop_distance_pct: float = 8.0
    candle_limit: int = 300
    benchmark_symbol: str = "SPY"

    # Regime / best-practice filters
    htf_bias_filter: bool = True
    min_adx: float = 20.0
    rsi_overextended: float = 75.0

    # Risk management (position sizing shown when account_equity > 0)
    account_equity: float = 0.0
    risk_per_trade_pct: float = 1.0

    # Custom TradingView indicator scoring rules + live signal journal
    custom_indicators_path: str = "config/custom_indicators.json"
    signal_journal_path: str = "data/signals.jsonl"

    # Backtest defaults
    backtest_fee_bps: float = 10.0
    backtest_slippage_bps: float = 5.0
    backtest_time_stop_bars: int = 40

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        ids = {p.strip() for p in self.telegram_allowed_user_ids.split(",")}
        return frozenset(int(p) for p in ids if p)

    @property
    def webhook_ip_allowlist(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.tv_webhook_ip_allowlist.split(",") if p.strip())

    @property
    def stock_exchange_candidates(self) -> tuple[str, ...]:
        return tuple(p.strip().upper() for p in self.tv_scanner_default_stock_exchanges.split(",") if p.strip())

    @property
    def tv_session_configured(self) -> bool:
        return bool(self.tv_session_id or (self.tv_username and self.tv_password))

    @property
    def nansen_active(self) -> bool:
        return self.nansen_mode != "off" and self.nansen_api_key is not None

    @property
    def execution_live(self) -> bool:
        return self.execution_enabled and not self.dry_run and bool(self.execution_webhook_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
