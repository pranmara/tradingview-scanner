from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.asset_classifier import AssetInfo
from app.config import Settings
from app.custom_indicators import CustomIndicatorRules, IndicatorRule
from app.schemas import (
    AssetClass,
    BucketScore,
    ConfluenceReport,
    Levels,
    OnChainSnapshot,
    PineAlert,
    RelativeStrength,
    Side,
    Signal,
    Timeframe,
    TimeframeAnalysis,
)

# Base matrix = 100 points of TradingView-derived evidence. Context (Nansen for crypto, volume profile / relative
# strength for stocks) is a bonus credit of up to CONTEXT_MAX applied only when that data is active and available.
TREND_MAX, MOMENTUM_MAX, INSTITUTIONAL_MAX, INDICATORS_MAX, EXECUTION_MAX = 30.0, 25.0, 20.0, 15.0, 10.0
CONTEXT_MAX = 10.0
TP_MULTIPLES = (1.5, 2.5, 4.0)
_TF_ORDER = (Timeframe.W1, Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M30, Timeframe.M15, Timeframe.M5)
_INTRADAY = (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1)
_KILL_ZONES_UTC = ((7, 10), (12, 15))  # London open, New York open


def management_plan(time_stop_bars: int, risk_pct: float) -> list[str]:
    """Flat exit at TP2, not the scale-out. Measured on 2,884 backtested trades across 8 markets and 8
    configurations, a single exit at TP2 beat scaling out in 8 of 8 comparisons, in-sample and out — the
    scale-out's move to breakeven after TP1 converts trades that would reach TP2 into zeros. Worth about
    0.2R per trade. See docs/CALIBRATION.md; neither plan makes the system profitable on that evidence.
    """
    return [
        f"Risk {risk_pct:g}% of equity per trade; size = risk ÷ (entry − SL)",
        "TP2 (2.5R): close the full position — a single exit measured better than scaling out",
        "Leave SL at its original level until TP2; moving it to breakeven at TP1 cost ~0.2R per trade",
        f"Time stop: exit at market if neither SL nor TP2 hits within {time_stop_bars} bars",
        "Invalidate early on an opposite MSB/CHoCH on the primary timeframe",
        "Backtest your own instruments before sizing up: `python -m app.backtest <SYM> --tf 4h --bars 3000`",
    ]


@dataclass
class ScanInputs:
    asset: AssetInfo
    primary: Timeframe
    analyses: dict[Timeframe, TimeframeAnalysis]
    onchain: OnChainSnapshot | None = None
    relative_strength: RelativeStrength | None = None
    alerts: list[PineAlert] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    now_ms: int | None = None
    extra_rules: dict[str, IndicatorRule] = field(default_factory=dict)


@dataclass
class _Bucket:
    score: BucketScore
    veto_long: list[str] = field(default_factory=list)
    veto_short: list[str] = field(default_factory=list)
    caution_long: list[str] = field(default_factory=list)
    caution_short: list[str] = field(default_factory=list)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6g}"


class DecisionEngine:
    def __init__(self, settings: Settings, rules: CustomIndicatorRules | None = None) -> None:
        self._s = settings
        self._rules = rules or CustomIndicatorRules({})

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tf_weights(primary: Timeframe, available: Iterable[Timeframe]) -> dict[Timeframe, float]:
        raw = {tf: (0.5 if tf == primary else 0.25) for tf in available}
        total = sum(raw.values()) or 1.0
        return {tf: w / total for tf, w in raw.items()}

    @staticmethod
    def _higher_timeframe(analyses: dict[Timeframe, TimeframeAnalysis], primary: Timeframe) -> Timeframe | None:
        rank = _TF_ORDER.index(primary)
        for tf in _TF_ORDER[:rank]:
            if tf in analyses:
                return tf
        return None

    # ------------------------------------------------------------------ buckets
    def _trend_bucket(self, analyses: dict[Timeframe, TimeframeAnalysis], primary: Timeframe) -> _Bucket:
        weights = self._tf_weights(primary, analyses)
        bull = bear = 0.0
        notes: list[str] = []
        for tf, ta in analyses.items():
            if ta.ribbon == "bullish":
                bull += 18.0 * weights[tf]
                notes.append(f"▲ {tf.value} EMA ribbon aligned bullish (20>50>200)")
            elif ta.ribbon == "bearish":
                bear += 18.0 * weights[tf]
                notes.append(f"▼ {tf.value} EMA ribbon aligned bearish (20<50<200)")

        structure_tfs = [tf for tf in (Timeframe.H1, Timeframe.H4) if tf in analyses] or [primary]
        per_tf = 12.0 / len(structure_tfs)
        for tf in structure_tfs:
            s = analyses[tf].structure
            if s.msb_bullish:
                bull += per_tf
                notes.append(f"▲ {tf.value} bullish MSB (swing high broken)")
            elif s.choch_bullish:
                bull += per_tf * 0.7
                notes.append(f"▲ {tf.value} bullish CHoCH (downtrend structure broken)")
            elif s.trend == "bullish":
                bull += per_tf * 0.35
            if s.msb_bearish:
                bear += per_tf
                notes.append(f"▼ {tf.value} bearish MSB (swing low broken)")
            elif s.choch_bearish:
                bear += per_tf * 0.7
                notes.append(f"▼ {tf.value} bearish CHoCH (uptrend structure broken)")
            elif s.trend == "bearish":
                bear += per_tf * 0.35

        return _Bucket(BucketScore(key="trend", name="Trend & Structure", max_points=TREND_MAX,
                                   bullish=_clamp(bull, 0, TREND_MAX), bearish=_clamp(bear, 0, TREND_MAX), notes=notes))

    def _momentum_bucket(self, analyses: dict[Timeframe, TimeframeAnalysis], primary: Timeframe) -> _Bucket:
        weights = self._tf_weights(primary, analyses)
        bull = bear = 0.0
        notes: list[str] = []
        for tf, ta in analyses.items():
            if ta.hidden_div_bullish:
                bull += 15.0 * weights[tf]
                notes.append(f"▲ {tf.value} RSI hidden bullish divergence (price HL / RSI LL)")
            if ta.hidden_div_bearish:
                bear += 15.0 * weights[tf]
                notes.append(f"▼ {tf.value} RSI hidden bearish divergence (price LH / RSI HH)")

        p = analyses.get(primary) or next(iter(analyses.values()))
        if p.bb_squeeze and p.volume_expansion:
            if p.last_bar_bullish:
                bull += 10.0
                notes.append(f"▲ {p.timeframe.value} BB squeeze (BBW {p.bbw:.3f}) + volume {p.volume_ratio:.1f}x, bullish bar")
            else:
                bear += 10.0
                notes.append(f"▼ {p.timeframe.value} BB squeeze (BBW {p.bbw:.3f}) + volume {p.volume_ratio:.1f}x, bearish bar")
        elif p.volume_expansion:
            if p.last_bar_bullish:
                bull += 4.0
            else:
                bear += 4.0
            notes.append(f"• {p.timeframe.value} volume expansion {p.volume_ratio:.1f}x 20-SMA")
        elif p.bb_squeeze:
            bull += 3.0
            bear += 3.0
            notes.append(f"• {p.timeframe.value} BB squeeze (BBW {p.bbw:.3f}) — breakout pending, direction unknown")

        return _Bucket(BucketScore(key="momentum", name="Momentum & Volatility", max_points=MOMENTUM_MAX,
                                   bullish=_clamp(bull, 0, MOMENTUM_MAX), bearish=_clamp(bear, 0, MOMENTUM_MAX), notes=notes))

    def _indicators_bucket(self, analyses: dict[Timeframe, TimeframeAnalysis], primary: Timeframe,
                           has_pine: bool = False) -> _Bucket:
        """TradingView's own technical rating (Recommend.All) per timeframe; custom Pine contributions land here too."""
        weights = self._tf_weights(primary, analyses)
        bull = bear = 0.0
        rated = False
        notes: list[str] = []
        for tf, ta in analyses.items():
            snap = ta.snapshot
            if snap is None or snap.recommend_all is None:
                continue
            rated = True
            r = snap.recommend_all
            pts = 7.5 * _clamp(abs(r) / 0.5, 0, 1) * weights[tf]
            if r > 0.1:
                bull += pts
                notes.append(f"▲ {tf.value} TradingView rating {snap.rating_label} ({r:+.2f})")
            elif r < -0.1:
                bear += pts
                notes.append(f"▼ {tf.value} TradingView rating {snap.rating_label} ({r:+.2f})")
        # With neither a rating nor a Pine reading the bucket has no evidence at all, so it drops out of the
        # denominator exactly as the institutional and context buckets already do. Keeping it in was silently
        # costing every score 15 points — which is why a backtest (never has snapshots) scored below a live scan.
        if not rated and not has_pine:
            notes.append("• No TradingView rating or Pine reading available")
        return _Bucket(BucketScore(key="indicators", name="TradingView Indicators", max_points=INDICATORS_MAX,
                                   bullish=_clamp(bull, 0, INDICATORS_MAX), bearish=_clamp(bear, 0, INDICATORS_MAX),
                                   available=rated or has_pine, notes=notes))

    def _institutional_bucket(self, analyses: dict[Timeframe, TimeframeAnalysis], primary: Timeframe) -> _Bucket:
        """VWAP benchmark, liquidity sweeps, imbalances (FVG), order blocks, premium/discount — per TF, weighted."""
        name = "Institutional Flow (VWAP / Liquidity / Imbalance)"
        with_signals = {tf: ta for tf, ta in analyses.items() if ta.institutional is not None}
        if not with_signals:
            return _Bucket(BucketScore(key="institutional", name=name, max_points=INSTITUTIONAL_MAX, bullish=0, bearish=0,
                                       available=False, notes=["• Institutional footprints unavailable (no candles)"]))
        weights = self._tf_weights(primary, with_signals)
        bull = bear = 0.0
        notes: list[str] = []
        for tf, ta in with_signals.items():
            ins = ta.institutional
            assert ins is not None
            w = weights[tf]
            close, atr_v = ta.close, ta.atr

            above, rising = close > ins.vwap, ins.vwap_slope_pct > 0
            if above and rising:
                bull += 5.0 * w
                notes.append(f"▲ {tf.value} above rising anchored VWAP ({ins.price_vs_vwap_pct:+.2f}%)")
            elif not above and not rising:
                bear += 5.0 * w
                notes.append(f"▼ {tf.value} below falling anchored VWAP ({ins.price_vs_vwap_pct:+.2f}%)")
            elif above:
                bull += 2.5 * w
            else:
                bear += 2.5 * w

            if ins.sweep_bullish_level is not None:
                bull += 6.0 * w
                notes.append(f"▲ {tf.value} liquidity sweep: stops below {_fmt(ins.sweep_bullish_level)} taken, closed back above")
            if ins.sweep_bearish_level is not None:
                bear += 6.0 * w
                notes.append(f"▼ {tf.value} liquidity sweep: stops above {_fmt(ins.sweep_bearish_level)} taken, closed back below")

            fvg = ins.fvg_bullish
            if fvg is not None:
                near = fvg.contains(close) or 0 <= close - fvg.high <= 1.5 * atr_v
                bull += (4.0 if near else 1.0) * w
                if near:
                    notes.append(f"▲ {tf.value} price at bullish imbalance {_fmt(fvg.low)}–{_fmt(fvg.high)}{' (tested)' if fvg.tested else ''}")
            fvg = ins.fvg_bearish
            if fvg is not None:
                near = fvg.contains(close) or 0 <= fvg.low - close <= 1.5 * atr_v
                bear += (4.0 if near else 1.0) * w
                if near:
                    notes.append(f"▼ {tf.value} price at bearish imbalance {_fmt(fvg.low)}–{_fmt(fvg.high)}{' (tested)' if fvg.tested else ''}")

            ob = ins.order_block_bullish
            if ob is not None:
                near = ob.contains(close) or 0 <= close - ob.high <= 1.0 * atr_v
                bull += (3.0 if near else 1.0) * w
                if near:
                    notes.append(f"▲ {tf.value} retesting bullish order block {_fmt(ob.low)}–{_fmt(ob.high)}")
            ob = ins.order_block_bearish
            if ob is not None:
                near = ob.contains(close) or 0 <= ob.low - close <= 1.0 * atr_v
                bear += (3.0 if near else 1.0) * w
                if near:
                    notes.append(f"▼ {tf.value} retesting bearish order block {_fmt(ob.low)}–{_fmt(ob.high)}")

            if ins.in_discount:
                bull += 2.0 * w
                notes.append(f"▲ {tf.value} in discount ({ins.range_position_pct:.0f}% of dealing range)")
            elif ins.in_premium:
                bear += 2.0 * w
                notes.append(f"▼ {tf.value} in premium ({ins.range_position_pct:.0f}% of dealing range)")

        return _Bucket(BucketScore(key="institutional", name=name, max_points=INSTITUTIONAL_MAX,
                                   bullish=_clamp(bull, 0, INSTITUTIONAL_MAX), bearish=_clamp(bear, 0, INSTITUTIONAL_MAX), notes=notes))

    def _onchain_bucket(self, oc: OnChainSnapshot | None) -> _Bucket:
        mode = self._s.nansen_mode
        name = f"On-Chain credit (Nansen, {mode})"
        if mode == "off":
            return _Bucket(BucketScore(key="context", name=name, max_points=CONTEXT_MAX, bullish=0, bearish=0, bonus=True,
                                       available=False, notes=["• Nansen disabled (NANSEN_MODE=off) — no on-chain credit"]))
        if oc is None or not oc.has_data:
            return _Bucket(BucketScore(key="context", name=name, max_points=CONTEXT_MAX, bullish=0, bearish=0, bonus=True,
                                       available=False, notes=["• Nansen data unavailable — no on-chain credit applied"]))
        bull = bear = max_pts = 0.0
        notes: list[str] = []
        flags_long: list[str] = []
        flags_short: list[str] = []

        nf = oc.sm_netflow_24h_usd
        if nf is not None:
            max_pts += 4.0
            pts = 4.0 * _clamp(abs(nf) / self._s.nansen_sm_netflow_full_score_usd, 0, 1)
            if nf > 0:
                bull += pts
                notes.append(f"▲ Smart Money 24h netflow +${nf:,.0f}")
            elif nf < 0:
                bear += pts
                notes.append(f"▼ Smart Money 24h netflow -${abs(nf):,.0f}")
            if nf < 0:
                flags_long.append(f"Smart Money 24h netflow negative (${nf:,.0f})")
            elif nf > 0:
                flags_short.append(f"Smart Money 24h netflow positive (+${nf:,.0f})")

        ex = oc.exchange_netflow_24h_usd
        if ex is not None:
            max_pts += 4.0
            pts = 4.0 * _clamp(abs(ex) / self._s.nansen_exchange_inflow_veto_usd, 0, 1)
            if ex > 0:
                bear += pts
                notes.append(f"▼ Exchange net inflow +${ex:,.0f} (sell pressure)")
                if ex >= self._s.nansen_exchange_inflow_veto_usd:
                    flags_long.append(f"Exchange inflow ${ex:,.0f} above threshold")
            elif ex < 0:
                bull += pts
                notes.append(f"▲ Exchange net outflow ${abs(ex):,.0f} (accumulation)")
                if abs(ex) >= self._s.nansen_exchange_inflow_veto_usd:
                    flags_short.append(f"Exchange outflow ${abs(ex):,.0f} above threshold")

        chg = oc.top_holder_concentration_change_pct
        if chg is not None:
            max_pts += 2.0
            pts = 2.0 * _clamp(abs(chg) / 1.0, 0, 1)
            if chg > 0:
                bull += pts
                notes.append(f"▲ Top-10 holders accumulating ({chg:+.2f}% 24h)")
            elif chg < 0:
                bear += pts
                notes.append(f"▼ Top-10 holders distributing ({chg:+.2f}% 24h)")

        if max_pts == 0:
            return _Bucket(BucketScore(key="context", name=name, max_points=CONTEXT_MAX, bullish=0, bearish=0, bonus=True,
                                       available=False, notes=["• Nansen returned no usable metrics — no on-chain credit applied"]))
        bucket = _Bucket(BucketScore(key="context", name=name, max_points=max_pts, bullish=bull, bearish=bear, bonus=True, notes=notes))
        if mode == "strict":
            bucket.veto_long, bucket.veto_short = flags_long, flags_short
        else:
            bucket.caution_long, bucket.caution_short = flags_long, flags_short
        return bucket

    def _stock_context_bucket(self, p: TimeframeAnalysis, rs: RelativeStrength | None) -> _Bucket:
        name = "Volume Profile & Relative Strength credit"
        bull = bear = max_pts = 0.0
        notes: list[str] = []
        vp = p.volume_profile
        if vp is not None:
            max_pts += 5.0
            if p.close > vp.vah:
                bull += 5.0
                notes.append(f"▲ Price above value area high ({vp.vah:,.2f}) — acceptance above value")
            elif p.close < vp.val:
                bear += 5.0
                notes.append(f"▼ Price below value area low ({vp.val:,.2f}) — rejection below value")
            elif p.close >= vp.poc:
                bull += 2.5
                notes.append(f"▲ Price above POC ({vp.poc:,.2f}) inside value area")
            else:
                bear += 2.5
                notes.append(f"▼ Price below POC ({vp.poc:,.2f}) inside value area")
        if rs is not None:
            max_pts += 5.0
            pts = 5.0 * _clamp(abs(rs.delta_pct) / 5.0, 0, 1)
            if rs.delta_pct > 0:
                bull += pts
                notes.append(f"▲ Outperforming {rs.benchmark} by {rs.delta_pct:+.2f}% over 20 bars")
            elif rs.delta_pct < 0:
                bear += pts
                notes.append(f"▼ Underperforming {rs.benchmark} by {rs.delta_pct:+.2f}% over 20 bars")
        if max_pts == 0:
            return _Bucket(BucketScore(key="context", name=name, max_points=CONTEXT_MAX, bullish=0, bearish=0, bonus=True,
                                       available=False, notes=["• Volume profile / benchmark unavailable — no credit applied"]))
        return _Bucket(BucketScore(key="context", name=name, max_points=max_pts, bullish=bull, bearish=bear, bonus=True, notes=notes))

    # ------------------------------------------------------------------ filters
    def _regime_vetoes(self, direction: Side, analyses: dict[Timeframe, TimeframeAnalysis], primary: Timeframe) -> list[str]:
        p = analyses[primary]
        out: list[str] = []
        if self._s.htf_bias_filter:
            htf = self._higher_timeframe(analyses, primary)
            if htf is not None:
                h = analyses[htf].ribbon
                if direction is Side.BUY and h == "bearish":
                    out.append(f"{htf.value} EMA bias is bearish — no longs against the higher-timeframe trend")
                if direction is Side.SELL and h == "bullish":
                    out.append(f"{htf.value} EMA bias is bullish — no shorts against the higher-timeframe trend")
        if p.adx is not None and p.adx < self._s.min_adx:
            out.append(f"{primary.value} ADX {p.adx:.1f} < {self._s.min_adx:g} — no trend regime (chop filter)")
        if direction is Side.BUY and p.rsi >= self._s.rsi_overextended:
            out.append(f"{primary.value} RSI {p.rsi:.0f} overextended (≥ {self._s.rsi_overextended:g}) — do not chase")
        if direction is Side.SELL and p.rsi <= 100.0 - self._s.rsi_overextended:
            out.append(f"{primary.value} RSI {p.rsi:.0f} oversold (≤ {100.0 - self._s.rsi_overextended:g}) — do not chase")
        return out

    # ------------------------------------------------------------------ levels
    def _levels(self, side: Side, p: TimeframeAnalysis) -> Levels | None:
        entry, atr_v, s, ins = p.close, p.atr, p.structure, p.institutional
        mult = self._s.atr_multiplier
        if atr_v <= 0 or entry <= 0:
            return None
        if side is Side.BUY:
            swing = s.last_swing_low if s.last_swing_low is not None and s.last_swing_low < entry else None
            sweep = ins.sweep_bullish_level if ins is not None and ins.sweep_bullish_level is not None and ins.sweep_bullish_level < entry else None
            if sweep is not None and entry - (sweep - 0.5 * atr_v) >= 0.5 * atr_v:
                sl, basis = sweep - 0.5 * atr_v, "below swept liquidity − 0.5×ATR"
            elif swing is not None:
                sl, basis = swing - mult * atr_v, f"swing low − {mult:g}×ATR"
            else:
                sl, basis = entry - 2.0 * atr_v, "entry − 2×ATR (no swing low)"
            if sl >= entry:
                sl, basis = entry - mult * atr_v, f"entry − {mult:g}×ATR"
            risk = entry - sl
            tps = [entry + risk * k for k in TP_MULTIPLES]
            candidates = [s.last_swing_high] if s.last_swing_high is not None else []
            if ins is not None:
                candidates += ins.equal_highs
            above = [c for c in candidates if c > entry]
            target = min(above) if above else None
            rrr_struct = (target - entry) / risk if target is not None else None
        else:
            swing = s.last_swing_high if s.last_swing_high is not None and s.last_swing_high > entry else None
            sweep = ins.sweep_bearish_level if ins is not None and ins.sweep_bearish_level is not None and ins.sweep_bearish_level > entry else None
            if sweep is not None and (sweep + 0.5 * atr_v) - entry >= 0.5 * atr_v:
                sl, basis = sweep + 0.5 * atr_v, "above swept liquidity + 0.5×ATR"
            elif swing is not None:
                sl, basis = swing + mult * atr_v, f"swing high + {mult:g}×ATR"
            else:
                sl, basis = entry + 2.0 * atr_v, "entry + 2×ATR (no swing high)"
            if sl <= entry:
                sl, basis = entry + mult * atr_v, f"entry + {mult:g}×ATR"
            risk = sl - entry
            tps = [entry - risk * k for k in TP_MULTIPLES]
            candidates = [s.last_swing_low] if s.last_swing_low is not None else []
            if ins is not None:
                candidates += ins.equal_lows
            below = [c for c in candidates if c < entry]
            target = max(below) if below else None
            rrr_struct = (entry - target) / risk if target is not None else None
        if risk <= 0:
            return None
        effective = min(TP_MULTIPLES[1], rrr_struct) if rrr_struct is not None else TP_MULTIPLES[1]

        risk_amount = units = notional = None
        if self._s.account_equity > 0:
            risk_amount = self._s.account_equity * self._s.risk_per_trade_pct / 100.0
            units = risk_amount / risk
            notional = units * entry

        return Levels(
            side=side, entry=entry, stop_loss=sl, tp1=tps[0], tp2=tps[1], tp3=tps[2],
            risk_per_unit=risk, stop_distance_pct=risk / entry * 100.0, stop_basis=basis, rrr_tp2=TP_MULTIPLES[1],
            structural_target=target, rrr_structural=rrr_struct, effective_rrr=effective,
            risk_amount=risk_amount, position_units=units, position_notional=notional,
        )

    def _execution_points(self, lv: Levels | None) -> tuple[float, list[str], list[str]]:
        if lv is None:
            return 0.0, ["• Could not compute levels (ATR/structure missing)"], ["Levels unavailable"]
        vetoes: list[str] = []
        notes: list[str] = []
        if lv.stop_distance_pct > self._s.max_stop_distance_pct:
            vetoes.append(f"Stop distance {lv.stop_distance_pct:.1f}% exceeds {self._s.max_stop_distance_pct:.0f}% cap")
            return 0.0, notes, vetoes
        pts = EXECUTION_MAX * _clamp(lv.effective_rrr / self._s.min_rrr, 0, 1)
        if lv.rrr_structural is not None and lv.rrr_structural < self._s.min_rrr:
            notes.append(f"• Nearest structural target caps RRR at {lv.rrr_structural:.2f} (< {self._s.min_rrr})")
        return pts, notes, vetoes

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, inputs: ScanInputs) -> ConfluenceReport:
        analyses = inputs.analyses
        primary = inputs.primary if inputs.primary in analyses else next(iter(analyses))
        p = analyses[primary]
        now_ms = inputs.now_ms if inputs.now_ms is not None else int(time.time() * 1000)

        # Resolved before the buckets are built: a Pine contribution is evidence the indicators bucket exists.
        contributions = list(self._rules.merged(inputs.extra_rules).contributions(inputs.alerts, now_ms))

        trend = self._trend_bucket(analyses, primary)
        momentum = self._momentum_bucket(analyses, primary)
        institutional = self._institutional_bucket(analyses, primary)
        indicators = self._indicators_bucket(analyses, primary,
                                             has_pine=any(c.bucket == "indicators" for c in contributions))
        context = self._onchain_bucket(inputs.onchain) if inputs.asset.is_crypto \
            else self._stock_context_bucket(p, inputs.relative_strength)

        by_key = {"trend": trend.score, "momentum": momentum.score, "indicators": indicators.score, "context": context.score}
        for c in contributions:
            b = by_key[c.bucket]
            if not b.available:
                b.notes.append(f"• {c.note} ignored — {b.name} bucket unavailable")
                continue
            b.bullish = _clamp(b.bullish + c.bullish, 0, b.max_points)
            b.bearish = _clamp(b.bearish + c.bearish, 0, b.max_points)
            b.notes.append(c.note)

        long_levels = self._levels(Side.BUY, p)
        short_levels = self._levels(Side.SELL, p)
        long_exec, long_notes, long_exec_veto = self._execution_points(long_levels)
        short_exec, short_notes, short_exec_veto = self._execution_points(short_levels)
        execution = BucketScore(key="execution", name="Execution Risk (ATR SL / RRR)", max_points=EXECUTION_MAX,
                                bullish=long_exec, bearish=short_exec, notes=[])

        base = [trend.score, momentum.score, institutional.score, indicators.score, execution]
        buckets = base + [context.score]
        total_max = sum(b.max_points for b in base if b.available) or 1.0
        bull_base = sum(b.bullish for b in base if b.available) / total_max * 100.0
        bear_base = sum(b.bearish for b in base if b.available) / total_max * 100.0
        # Context is a credit: it is added on top only when active/available and can never be required to reach 100.
        bonus_bull = context.score.bullish if context.score.available else 0.0
        bonus_bear = context.score.bearish if context.score.available else 0.0
        bull_score = round(min(100.0, bull_base + bonus_bull), 1)
        bear_score = round(min(100.0, bear_base + bonus_bear), 1)
        coverage = round(total_max, 1)

        direction: Side | None
        if bull_score > bear_score:
            direction, score = Side.BUY, bull_score
        elif bear_score > bull_score:
            direction, score = Side.SELL, bear_score
        else:
            direction, score = None, bull_score

        vetoes: list[str] = []
        cautions: list[str] = []
        levels: Levels | None = None
        parts = (trend, momentum, institutional, indicators, context)
        if direction is Side.BUY:
            levels = long_levels
            vetoes = [v for b in parts for v in b.veto_long] + long_exec_veto
            cautions = [c for b in parts for c in b.caution_long]
            execution.notes = long_notes
        elif direction is Side.SELL:
            levels = short_levels
            vetoes = [v for b in parts for v in b.veto_short] + short_exec_veto
            cautions = [c for b in parts for c in b.caution_short]
            execution.notes = short_notes
        if direction is not None:
            vetoes += self._regime_vetoes(direction, analyses, primary)
            if p.degraded:
                vetoes.append(f"{primary.value} analysis is snapshot-only (no candles) — no structure or divergence confirmation")
            if self._s.session_filter and primary in _INTRADAY:
                hour = time.gmtime(now_ms / 1000).tm_hour
                if not any(lo <= hour < hi for lo, hi in _KILL_ZONES_UTC):
                    cautions.append(f"Outside London/NY kill zones (UTC 07–10, 12–15; now {hour:02d}h) — thinner institutional participation")

        signal = Signal.NEUTRAL
        if direction is not None:
            rrr_ok = levels is not None and levels.effective_rrr >= self._s.min_rrr
            if score >= self._s.min_signal_score and rrr_ok and not vetoes:
                signal = Signal.BUY if direction is Side.BUY else Signal.SELL
            elif score >= self._s.watch_score:
                signal = Signal.WATCH
            if not rrr_ok and levels is not None and score >= self._s.min_signal_score:
                vetoes.append(f"Effective RRR {levels.effective_rrr:.2f} below minimum {self._s.min_rrr}")

        marker = "▲" if direction is Side.BUY else "▼"
        reasons = [n for b in buckets for n in b.notes if n.startswith(marker) or n.startswith("•")]

        regime_parts = []
        if p.adx is not None:
            regime_parts.append(f"ADX {p.adx:.0f} ({'trending' if p.adx >= self._s.min_adx else 'chop'})")
        regime_parts.append(f"RSI {p.rsi:.0f}")
        htf = self._higher_timeframe(analyses, primary)
        if htf is not None:
            regime_parts.append(f"{htf.value} bias {analyses[htf].ribbon}")

        return ConfluenceReport(
            symbol=inputs.asset.symbol,
            asset_class=inputs.asset.asset_class,
            primary_timeframe=primary,
            signal=signal,
            direction=direction,
            score=score,
            bullish_score=bull_score,
            bearish_score=bear_score,
            coverage_pct=coverage,
            buckets=buckets,
            levels=levels,
            reasons=reasons,
            vetoes=vetoes,
            cautions=cautions,
            management=management_plan(self._s.backtest_time_stop_bars, self._s.risk_per_trade_pct) if levels else [],
            regime=" · ".join(regime_parts),
            data_sources=sorted(set(inputs.data_sources)),
            pine_alerts=inputs.alerts[:10],
            onchain=inputs.onchain,
            relative_strength=inputs.relative_strength,
            institutional=p.institutional,
            timeframes_analyzed=list(analyses.keys()),
            errors=inputs.errors,
        )


__all__ = ["DecisionEngine", "ScanInputs", "AssetClass", "management_plan"]
