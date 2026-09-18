from __future__ import annotations

import time

from app.asset_classifier import classify
from app.config import Settings
from app.decision_engine import DecisionEngine, ScanInputs
from app.schemas import (
    AlertSignal,
    InstitutionalSignals,
    MarketStructure,
    OnChainSnapshot,
    PineAlert,
    RelativeStrength,
    Side,
    Signal,
    TechnicalSnapshot,
    Timeframe,
    TimeframeAnalysis,
    VolumeProfile,
    Zone,
)


def _institutional(bullish: bool, close: float) -> InstitutionalSignals:
    if bullish:
        return InstitutionalSignals(
            vwap=close - 1, vwap_anchor="swing low 20 bars ago", vwap_slope_pct=0.4, vwap_upper_2=close + 3, vwap_lower_2=close - 5,
            price_vs_vwap_pct=1.0, sweep_bullish_level=close - 1.5,
            fvg_bullish=Zone(low=close - 1.2, high=close - 0.8, index=290, kind="fvg"),
            order_block_bullish=Zone(low=close - 2.5, high=close - 1.6, index=280, kind="order_block"),
            range_low=close - 10, range_high=close + 2, range_position_pct=30.0,
        )
    return InstitutionalSignals(
        vwap=close + 1, vwap_anchor="swing high 20 bars ago", vwap_slope_pct=-0.4, vwap_upper_2=close + 5, vwap_lower_2=close - 3,
        price_vs_vwap_pct=-1.0, sweep_bearish_level=close + 1.5,
        fvg_bearish=Zone(low=close + 0.8, high=close + 1.2, index=290, kind="fvg"),
        order_block_bearish=Zone(low=close + 1.6, high=close + 2.5, index=280, kind="order_block"),
        range_low=close - 2, range_high=close + 10, range_position_pct=70.0,
    )


def _analysis(tf: Timeframe, bullish: bool = True, close: float = 100.0, atr: float = 1.0) -> TimeframeAnalysis:
    return TimeframeAnalysis(
        institutional=_institutional(bullish, close),
        timeframe=tf, bars=300, source="test", close=close,
        snapshot=TechnicalSnapshot(symbol="X", timeframe=tf, source="tv-scanner", recommend_all=0.6 if bullish else -0.6),
        ema20=close - 1 if bullish else close + 1,
        ema50=close - 2 if bullish else close + 2,
        ema200=close - 3 if bullish else close + 3,
        ribbon="bullish" if bullish else "bearish",
        rsi=58.0 if bullish else 42.0, adx=28.0, bbw=0.03, bb_squeeze=True, volume_ratio=1.8, volume_expansion=True, atr=atr,
        structure=MarketStructure(
            trend="bullish" if bullish else "bearish",
            msb_bullish=bullish, msb_bearish=not bullish,
            last_swing_high=close + 12.0, last_swing_low=close - 2.0,
        ),
        hidden_div_bullish=bullish, hidden_div_bearish=not bullish,
        volume_profile=VolumeProfile(poc=close - 5, vah=close - 1, val=close - 9),
        return_20_pct=8.0, last_bar_bullish=bullish,
    )


def _all_bullish() -> dict[Timeframe, TimeframeAnalysis]:
    return {tf: _analysis(tf) for tf in (Timeframe.H4, Timeframe.H1, Timeframe.D1)}


def test_full_bullish_crypto_confluence_yields_buy(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    onchain = OnChainSnapshot(chain="ethereum", token_address="0x", source="nansen",
                              sm_netflow_24h_usd=2_000_000, exchange_netflow_24h_usd=-6_000_000,
                              top_holder_concentration_change_pct=1.5)
    alert = PineAlert(ticker="BTCUSDT", timeframe="4h", indicator="SuperTrend_V2", signal=AlertSignal.BUY,
                      price=100.0, received_at_ms=int(time.time() * 1000))
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish(),
                                        onchain=onchain, alerts=[alert], data_sources=["binance"]))
    assert report.direction is Side.BUY
    assert report.signal is Signal.BUY
    assert report.score >= settings.min_signal_score
    assert report.coverage_pct == 100
    assert report.levels is not None
    lv = report.levels
    assert lv.stop_loss == 100.0 - 1.5 - 0.5  # below the swept liquidity level − 0.5×ATR (institutional stop)
    assert "swept" in lv.stop_basis
    assert lv.tp2 - lv.entry == 2.5 * lv.risk_per_unit
    assert lv.effective_rrr >= settings.min_rrr
    inst = next(b for b in report.buckets if b.key == "institutional")
    assert inst.max_points == 20 and inst.bullish >= 17


def _negative_onchain() -> OnChainSnapshot:
    return OnChainSnapshot(chain="ethereum", token_address="0x", source="nansen",
                           sm_netflow_24h_usd=-500_000, exchange_netflow_24h_usd=-1_000_000)


def test_advisory_nansen_warns_but_does_not_block(settings: Settings) -> None:
    assert settings.nansen_mode == "advisory"
    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("ETHUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), onchain=_negative_onchain()))
    assert report.direction is Side.BUY
    assert report.signal in (Signal.BUY, Signal.WATCH)  # contrary on-chain data costs points but never vetoes
    assert not report.vetoes
    assert any("netflow" in c.lower() for c in report.cautions)


def test_strict_nansen_vetoes_long() -> None:
    s = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None, nansen_mode="strict")  # type: ignore[call-arg]
    report = DecisionEngine(s).evaluate(ScanInputs(asset=classify("ETHUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), onchain=_negative_onchain()))
    assert report.signal is not Signal.BUY
    assert any("netflow" in v.lower() for v in report.vetoes)


def test_nansen_off_gives_no_credit_and_full_coverage() -> None:
    s = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None, nansen_mode="off")  # type: ignore[call-arg]
    report = DecisionEngine(s).evaluate(ScanInputs(asset=classify("ETHUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), onchain=_negative_onchain()))
    assert report.coverage_pct == 100 and report.signal is Signal.BUY
    ctx = next(b for b in report.buckets if b.key == "context")
    assert ctx.bonus and not ctx.available


def test_missing_onchain_keeps_base_matrix_at_100(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), onchain=None))
    assert report.coverage_pct == 100  # Nansen absence never reduces coverage
    assert report.signal is Signal.BUY  # TA-only confluence is enough on its own
    onchain_bucket = next(b for b in report.buckets if b.key == "context")
    assert onchain_bucket.bonus and not onchain_bucket.available and "Nansen" in onchain_bucket.name


def test_nansen_credit_is_additive_and_capped(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    positive = OnChainSnapshot(chain="ethereum", token_address="0x", source="nansen",
                               sm_netflow_24h_usd=5_000_000, exchange_netflow_24h_usd=-9_000_000, top_holder_concentration_change_pct=2.0)
    base = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), onchain=None))
    credited = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), onchain=positive))
    ctx = next(b for b in credited.buckets if b.key == "context")
    assert ctx.available and ctx.bullish == 10.0
    assert credited.bullish_score == min(100.0, round(base.bullish_score + 10.0, 1))
    assert credited.coverage_pct == 100

    weak = {tf: _analysis(tf) for tf in (Timeframe.H4,)}
    weak[Timeframe.H4].institutional = None
    weak[Timeframe.H4].structure = MarketStructure(trend="ranging", last_swing_high=112.0, last_swing_low=98.0)
    lo = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=weak, onchain=None))
    hi = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=weak, onchain=positive))
    assert hi.bullish_score == round(lo.bullish_score + 10.0, 1)


def test_tradingview_rating_feeds_indicators_bucket(settings: Settings) -> None:
    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish()))
    ind = next(b for b in report.buckets if b.key == "indicators")
    assert ind.max_points == 15 and ind.bullish == 7.5
    assert any("TradingView rating STRONG_BUY" in n for n in ind.notes)


def test_pine_alert_defaults_to_indicators_bucket(settings: Settings) -> None:
    alert = PineAlert(ticker="BTCUSDT", timeframe="4h", indicator="Anything", signal=AlertSignal.BUY, price=100.0,
                      received_at_ms=int(time.time() * 1000))
    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish(), alerts=[alert]))
    ind = next(b for b in report.buckets if b.key == "indicators")
    assert ind.bullish == 12.5


def test_structural_target_caps_rrr(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    analyses = _all_bullish()
    for ta in analyses.values():
        ta.structure.last_swing_high = 103.0  # only 3 above entry → RRR < 2.5
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    assert report.levels is not None
    assert report.levels.rrr_structural is not None and report.levels.rrr_structural < settings.min_rrr
    assert report.signal is not Signal.BUY


def test_stock_uses_volume_profile_and_relative_strength(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    rs = RelativeStrength(benchmark="XLK", asset_return_pct=9.0, benchmark_return_pct=2.0)
    report = engine.evaluate(ScanInputs(asset=classify("AAPL"), primary=Timeframe.D1, analyses=_all_bullish(), relative_strength=rs))
    ctx = next(b for b in report.buckets if b.key == "context")
    assert ctx.available and ctx.bonus and ctx.max_points == 10 and ctx.bullish > 7
    assert report.direction is Side.BUY and report.coverage_pct == 100


def test_degraded_primary_is_vetoed_and_uses_atr_only_stop(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    analyses = _all_bullish()
    analyses[Timeframe.H4].degraded = True
    analyses[Timeframe.H4].structure = MarketStructure(trend="ranging")
    analyses[Timeframe.H4].institutional = None
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    assert report.signal is not Signal.BUY
    assert any("snapshot-only" in v for v in report.vetoes)
    assert report.levels is not None
    assert report.levels.stop_basis.startswith("entry")
    assert report.levels.stop_loss == 100.0 - 2.0 * 1.0


def test_htf_bias_filter_vetoes_counter_trend_long(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    analyses = _all_bullish()
    analyses[Timeframe.D1] = _analysis(Timeframe.D1, bullish=False)
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    assert report.direction is Side.BUY
    assert any("1d EMA bias is bearish" in v for v in report.vetoes)
    assert report.signal is not Signal.BUY


def test_adx_chop_and_rsi_overextension_filters(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    analyses = _all_bullish()
    analyses[Timeframe.H4].adx = 12.0
    analyses[Timeframe.H4].rsi = 81.0
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    assert any("ADX" in v for v in report.vetoes) and any("overextended" in v for v in report.vetoes)


def test_position_sizing_and_management_plan() -> None:
    s = Settings(_env_file=None, telegram_bot_token="1:x", tv_webhook_secret="s", redis_url=None,  # type: ignore[call-arg]
                 account_equity=10_000, risk_per_trade_pct=1.0)
    report = DecisionEngine(s).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=_all_bullish()))
    lv = report.levels
    assert lv is not None and lv.risk_amount == 100.0
    assert lv.position_units == 100.0 / lv.risk_per_unit
    assert report.management and "breakeven" in " ".join(report.management)


def test_custom_indicator_rule_feeds_trend_bucket(settings: Settings) -> None:
    from app.custom_indicators import CustomIndicatorRules, IndicatorRule

    rules = CustomIndicatorRules({"SuperTrend_V2": IndicatorRule(bucket="trend", points=6)})
    engine = DecisionEngine(settings, rules)
    analyses = {tf: _analysis(tf) for tf in (Timeframe.H4,)}
    analyses[Timeframe.H4].structure = MarketStructure(trend="ranging", last_swing_high=112.0, last_swing_low=98.0)
    alert = PineAlert(ticker="BTCUSDT", timeframe="4h", indicator="supertrend_v2", signal=AlertSignal.BUY,
                      price=100.0, received_at_ms=int(time.time() * 1000))
    without = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    with_alert = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses, alerts=[alert]))
    trend_a = next(b for b in without.buckets if b.key == "trend").bullish
    trend_b = next(b for b in with_alert.buckets if b.key == "trend").bullish
    assert trend_b == trend_a + 6


def test_liquidity_pool_becomes_structural_target(settings: Settings) -> None:
    analyses = _all_bullish()
    ta = analyses[Timeframe.H4]
    ta.structure.last_swing_high = 130.0
    assert ta.institutional is not None
    ta.institutional.equal_highs = [104.0, 111.0]   # nearest resting liquidity above price
    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    assert report.levels is not None and report.levels.structural_target == 104.0


def test_session_caution_outside_kill_zones(settings: Settings) -> None:
    analyses = {tf: _analysis(tf) for tf in (Timeframe.H1, Timeframe.H4, Timeframe.D1)}
    night = 1_800_000_000_000 - (1_800_000_000_000 % 86_400_000) + 3 * 3_600_000   # 03:00 UTC
    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H1, analyses=analyses, now_ms=night))
    assert any("kill zones" in c for c in report.cautions)
    day = night + 10 * 3_600_000  # 13:00 UTC, New York
    report = DecisionEngine(settings).evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H1, analyses=analyses, now_ms=day))
    assert not any("kill zones" in c for c in report.cautions)


def test_bearish_alignment_yields_sell_direction(settings: Settings) -> None:
    engine = DecisionEngine(settings)
    analyses = {tf: _analysis(tf, bullish=False) for tf in (Timeframe.H4, Timeframe.H1, Timeframe.D1)}
    for ta in analyses.values():
        ta.structure.last_swing_high = 102.0
        ta.structure.last_swing_low = 88.0
    report = engine.evaluate(ScanInputs(asset=classify("BTCUSDT"), primary=Timeframe.H4, analyses=analyses))
    assert report.direction is Side.SELL
    assert report.levels is not None and report.levels.stop_loss > report.levels.entry
    assert report.levels.stop_loss == 100.0 + 1.5 + 0.5 and "swept" in report.levels.stop_basis
