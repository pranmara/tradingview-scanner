from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    STOCK = "stock"


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WATCH = "WATCH"
    NEUTRAL = "NEUTRAL"


class AlertSignal(str, Enum):
    """Pine alert direction. NEUTRAL = state update carrying indicator values only."""

    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: int = Field(description="Bar open time, epoch milliseconds")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class OHLCV(BaseModel):
    symbol: str
    timeframe: Timeframe
    candles: list[Candle]
    source: str


class TechnicalSnapshot(BaseModel):
    symbol: str
    timeframe: Timeframe
    source: str
    close: float | None = None
    recommend_all: float | None = None
    recommend_ma: float | None = None
    recommend_osc: float | None = None
    rsi: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    atr: float | None = None
    volume: float | None = None
    sector: str | None = None

    @property
    def rating_label(self) -> str | None:
        if self.recommend_all is None:
            return None
        r = self.recommend_all
        if r >= 0.5:
            return "STRONG_BUY"
        if r >= 0.1:
            return "BUY"
        if r <= -0.5:
            return "STRONG_SELL"
        if r <= -0.1:
            return "SELL"
        return "NEUTRAL"


class MarketStructure(BaseModel):
    trend: Literal["bullish", "bearish", "ranging"]
    msb_bullish: bool = False
    msb_bearish: bool = False
    choch_bullish: bool = False
    choch_bearish: bool = False
    last_swing_high: float | None = None
    last_swing_low: float | None = None


class VolumeProfile(BaseModel):
    poc: float
    vah: float
    val: float


class Zone(BaseModel):
    low: float
    high: float
    index: int
    kind: Literal["fvg", "order_block"]
    tested: bool = False

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


class InstitutionalSignals(BaseModel):
    """Footprints of institutional execution derived from OHLCV: VWAP benchmark, liquidity sweeps, imbalances, order blocks."""

    vwap: float
    vwap_anchor: str
    vwap_slope_pct: float
    vwap_upper_2: float
    vwap_lower_2: float
    price_vs_vwap_pct: float
    sweep_bullish_level: float | None = None
    sweep_bearish_level: float | None = None
    fvg_bullish: Zone | None = None
    fvg_bearish: Zone | None = None
    order_block_bullish: Zone | None = None
    order_block_bearish: Zone | None = None
    equal_highs: list[float] = Field(default_factory=list)
    equal_lows: list[float] = Field(default_factory=list)
    range_low: float | None = None
    range_high: float | None = None
    range_position_pct: float | None = None

    @property
    def in_discount(self) -> bool:
        return self.range_position_pct is not None and self.range_position_pct < 40.0

    @property
    def in_premium(self) -> bool:
        return self.range_position_pct is not None and self.range_position_pct > 60.0


class TimeframeAnalysis(BaseModel):
    timeframe: Timeframe
    bars: int
    source: str
    close: float
    ema20: float
    ema50: float
    ema200: float
    ribbon: Literal["bullish", "bearish", "mixed"]
    rsi: float
    adx: float | None = None
    bbw: float
    bb_squeeze: bool
    volume_ratio: float
    volume_expansion: bool
    atr: float
    structure: MarketStructure
    hidden_div_bullish: bool
    hidden_div_bearish: bool
    volume_profile: VolumeProfile | None = None
    institutional: InstitutionalSignals | None = None
    return_20_pct: float | None = None
    last_bar_bullish: bool
    snapshot: TechnicalSnapshot | None = None
    degraded: bool = Field(default=False, description="Built from an indicator snapshot only; no candle-based structure")


class OnChainSnapshot(BaseModel):
    chain: str
    token_address: str
    source: str
    sm_netflow_24h_usd: float | None = None
    exchange_netflow_24h_usd: float | None = Field(
        default=None, description="Positive = net inflow to exchanges (sell pressure)"
    )
    top_holder_concentration_pct: float | None = None
    top_holder_concentration_change_pct: float | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_data(self) -> bool:
        return any(
            v is not None
            for v in (
                self.sm_netflow_24h_usd,
                self.exchange_netflow_24h_usd,
                self.top_holder_concentration_change_pct,
            )
        )


class RelativeStrength(BaseModel):
    benchmark: str
    asset_return_pct: float
    benchmark_return_pct: float

    @property
    def delta_pct(self) -> float:
        return self.asset_return_pct - self.benchmark_return_pct


class PineAlertIn(BaseModel):
    """Inbound TradingView alert payload (secret included)."""

    model_config = ConfigDict(extra="ignore")

    ticker: str = Field(min_length=1, max_length=40)
    timeframe: str = Field(min_length=1, max_length=10)
    indicator: str = Field(min_length=1, max_length=64)
    signal: AlertSignal
    price: float = Field(gt=0)
    secret_key: str = Field(min_length=1, max_length=256)
    timestamp: int | str | None = None
    values: dict[str, float] = Field(default_factory=dict, description="Optional numeric indicator readings")

    @field_validator("signal", mode="before")
    @classmethod
    def _upper_signal(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("values", mode="before")
    @classmethod
    def _numeric_values(cls, v: object) -> dict[str, float]:
        if not isinstance(v, dict):
            return {}
        out: dict[str, float] = {}
        for key, val in list(v.items())[:32]:
            if isinstance(val, bool) or not isinstance(val, (int, float, str)):
                continue
            try:
                num = float(val)
            except ValueError:
                continue
            if num == num and abs(num) != float("inf"):
                out[str(key)[:32]] = num
        return out

    def alert_epoch_ms(self) -> int | None:
        raw = self.timestamp
        if raw is None:
            return None
        if isinstance(raw, int):
            return raw if raw > 10**11 else raw * 1000
        text = raw.strip()
        if text.isdigit():
            val = int(text)
            return val if val > 10**11 else val * 1000
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)


class PineAlert(BaseModel):
    ticker: str
    timeframe: str
    indicator: str
    signal: AlertSignal
    price: float
    values: dict[str, float] = Field(default_factory=dict)
    alert_ts_ms: int | None = None
    received_at_ms: int


class BucketScore(BaseModel):
    key: str
    name: str
    max_points: float
    bullish: float
    bearish: float
    available: bool = True
    notes: list[str] = Field(default_factory=list)


class Levels(BaseModel):
    side: Side
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_per_unit: float
    stop_distance_pct: float
    stop_basis: str
    rrr_tp2: float
    structural_target: float | None = None
    rrr_structural: float | None = None
    effective_rrr: float
    risk_amount: float | None = None
    position_units: float | None = None
    position_notional: float | None = None


class ConfluenceReport(BaseModel):
    symbol: str
    asset_class: AssetClass
    primary_timeframe: Timeframe
    signal: Signal
    direction: Side | None
    score: float
    bullish_score: float
    bearish_score: float
    coverage_pct: float
    buckets: list[BucketScore]
    levels: Levels | None
    reasons: list[str] = Field(default_factory=list)
    vetoes: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list, description="Advisory warnings that do not block the signal")
    management: list[str] = Field(default_factory=list)
    regime: str | None = None
    data_sources: list[str] = Field(default_factory=list)
    pine_alerts: list[PineAlert] = Field(default_factory=list)
    onchain: OnChainSnapshot | None = None
    relative_strength: RelativeStrength | None = None
    institutional: InstitutionalSignals | None = None
    timeframes_analyzed: list[Timeframe] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionPayload(BaseModel):
    symbol: str
    asset_class: AssetClass
    side: Side
    timeframe: Timeframe
    entry: float
    stop_loss: float
    take_profits: list[float]
    score: float
    rrr: float
    position_units: float | None = None
    management: list[str] = Field(default_factory=list)
    idempotency_key: str
    generated_at: datetime
    dry_run: bool
