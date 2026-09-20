from __future__ import annotations

import html
import logging
import time
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

from app.alert_store import AlertStore
from app.clients.market_data import CompositeMarketDataProvider
from app.clients.nansen import NansenClient
from app.clients.tradingview_ws import ScriptInfo, TradingViewSessionClient
from app.config import Settings
from app.orchestrator import ScanError, ScanOrchestrator
from app.schemas import ConfluenceReport, Side, Signal, Timeframe
from app.study_advisor import Advice, StudyAdvisor
from app.study_registry import StudyRegistry, parse_kv
from app.timeframes import SUPPORTED_LABEL, parse_timeframe

logger = logging.getLogger(__name__)

_USAGE = (
    "<b>Usage</b>\n"
    "<code>/scan &lt;TICKER&gt; [TIMEFRAME]</code>\n"
    "Examples: <code>/scan BTCUSDT 4h</code>, <code>/scan AAPL 1d</code>, <code>/scan BINANCE:SOLUSDT 1w</code>\n"
    f"Timeframes: {SUPPORTED_LABEL} (default 4h)\n"
    "Prefix <code>crypto:</code> / <code>stock:</code> to force asset class.\n\n"
    "<b>Account indicators</b> (needs TV_SESSION_ID)\n"
    "<code>/indicators</code> — list scripts on your TradingView account\n"
    "<code>/indicators add &lt;n|pine_id&gt; [plot=plot_0 above=0 below=0 points=5 bucket=indicators age=2 in.Length=20]</code>\n"
    "With TYPESAFE_API_KEY set, <code>add</code> picks bucket / plot / thresholds / points from the script itself; "
    "any flag you pass still wins, and <code>auto=off</code> skips it.\n"
    "<code>/indicators active</code> · <code>/indicators remove &lt;name&gt;</code> · <code>/indicators clear</code>\n\n"
    "<code>/status</code> — upstream health"
)
_SIGNAL_ICON = {Signal.BUY: "🟢 BUY", Signal.SELL: "🔴 SELL", Signal.WATCH: "🟡 WATCH", Signal.NEUTRAL: "⚪ NEUTRAL"}


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6g}"


def _bar(score: float) -> str:
    filled = int(round(score / 10))
    return "▓" * filled + "░" * (10 - filled)


def format_report(r: ConfluenceReport) -> str:
    e = html.escape
    lines = [
        f"<b>{e(r.symbol)}</b> · {r.primary_timeframe.value} · {r.asset_class.value} — <b>{_SIGNAL_ICON[r.signal]}</b>",
        f"Score <b>{r.score:.0f}</b>/100 {_bar(r.score)}  (bull {r.bullish_score:.0f} / bear {r.bearish_score:.0f}, coverage {r.coverage_pct:.0f}%)",
        "",
        "<b>Confluence Matrix</b>",
    ]
    for b in r.buckets:
        pts = b.bullish if r.direction is Side.BUY else b.bearish if r.direction is Side.SELL else max(b.bullish, b.bearish)
        if b.bonus:
            shown = f"+{pts:.1f} credit (max {b.max_points:.0f})" if b.available else "no credit"
        else:
            shown = f"{pts:.1f}/{b.max_points:.0f}" if b.available else "n/a"
        lines.append(f"<code>{e(b.name[:34]):<34}</code> {shown}")

    lv = r.levels
    if lv is not None:
        sign = "-" if lv.side is Side.BUY else "+"
        lines += [
            "",
            f"<b>Levels ({lv.side.value})</b>",
            f"Entry  <code>{fmt_price(lv.entry)}</code>",
            f"SL     <code>{fmt_price(lv.stop_loss)}</code>  ({sign}{lv.stop_distance_pct:.2f}%, {e(lv.stop_basis)})",
            f"TP1    <code>{fmt_price(lv.tp1)}</code>  (1.5R)",
            f"TP2    <code>{fmt_price(lv.tp2)}</code>  (2.5R)",
            f"TP3    <code>{fmt_price(lv.tp3)}</code>  (4.0R)",
            f"RRR    <b>{lv.effective_rrr:.2f}</b>"
            + (f"  (structural target {fmt_price(lv.structural_target)})" if lv.structural_target is not None else ""),
        ]
        if lv.position_units is not None and lv.risk_amount is not None and lv.position_notional is not None:
            lines.append(f"Size   <code>{lv.position_units:.6g}</code> units ≈ ${lv.position_notional:,.0f}, risking ${lv.risk_amount:,.0f}")
    if r.regime:
        lines.append(f"Regime {e(r.regime)}")
    if r.management and r.signal in (Signal.BUY, Signal.SELL, Signal.WATCH):
        lines += ["", "<b>Trade plan</b>"] + [f"• {e(m)}" for m in r.management]

    if r.reasons:
        lines += ["", "<b>Confluence notes</b>"] + [e(n) for n in r.reasons[:12]]
    if r.vetoes:
        lines += ["", "<b>⛔ Vetoes</b>"] + [f"• {e(v)}" for v in r.vetoes]
    if r.cautions:
        lines += ["", "<b>⚠️ Cautions (advisory, not blocking)</b>"] + [f"• {e(c)}" for c in r.cautions]

    if r.onchain is not None and r.onchain.has_data:
        oc = r.onchain
        parts = []
        if oc.sm_netflow_24h_usd is not None:
            parts.append(f"SM netflow 24h: ${oc.sm_netflow_24h_usd:+,.0f}")
        if oc.exchange_netflow_24h_usd is not None:
            parts.append(f"Exchange netflow 24h: ${oc.exchange_netflow_24h_usd:+,.0f}")
        if oc.top_holder_concentration_pct is not None:
            parts.append(f"Top-10 holders: {oc.top_holder_concentration_pct:.1f}%")
        lines += ["", f"<b>On-chain ({e(oc.chain)})</b>", " · ".join(parts)]
    if r.institutional is not None:
        ins = r.institutional
        ins_lines = [f"VWAP {fmt_price(ins.vwap)} ({e(ins.vwap_anchor)}) · price {ins.price_vs_vwap_pct:+.2f}% · slope {'↑' if ins.vwap_slope_pct > 0 else '↓'}"]
        if ins.sweep_bullish_level is not None:
            ins_lines.append(f"Liquidity sweep ▲ below {fmt_price(ins.sweep_bullish_level)}")
        if ins.sweep_bearish_level is not None:
            ins_lines.append(f"Liquidity sweep ▼ above {fmt_price(ins.sweep_bearish_level)}")
        zones = []
        if ins.fvg_bullish is not None:
            zones.append(f"FVG▲ {fmt_price(ins.fvg_bullish.low)}–{fmt_price(ins.fvg_bullish.high)}")
        if ins.fvg_bearish is not None:
            zones.append(f"FVG▼ {fmt_price(ins.fvg_bearish.low)}–{fmt_price(ins.fvg_bearish.high)}")
        if ins.order_block_bullish is not None:
            zones.append(f"OB▲ {fmt_price(ins.order_block_bullish.low)}–{fmt_price(ins.order_block_bullish.high)}")
        if ins.order_block_bearish is not None:
            zones.append(f"OB▼ {fmt_price(ins.order_block_bearish.low)}–{fmt_price(ins.order_block_bearish.high)}")
        if zones:
            ins_lines.append(" · ".join(zones))
        if ins.range_position_pct is not None:
            pos = "discount" if ins.in_discount else "premium" if ins.in_premium else "equilibrium"
            ins_lines.append(f"Dealing range {fmt_price(ins.range_low or 0)}–{fmt_price(ins.range_high or 0)} · {ins.range_position_pct:.0f}% ({pos})")
        pools = [fmt_price(x) for x in ins.equal_highs[:2]] + [fmt_price(x) for x in ins.equal_lows[-2:]]
        if pools:
            ins_lines.append("Liquidity pools: " + ", ".join(pools))
        lines += ["", f"<b>Institutional ({r.primary_timeframe.value})</b>"] + ins_lines
    if r.relative_strength is not None:
        rs = r.relative_strength
        lines += ["", f"<b>Relative strength</b> vs {e(rs.benchmark)}: {rs.asset_return_pct:+.2f}% vs {rs.benchmark_return_pct:+.2f}% ({rs.delta_pct:+.2f}%)"]

    if r.pine_alerts:
        recent = ", ".join(
            f"{e(a.indicator)} {a.signal.value}@{e(a.timeframe)}"
            + (" [" + ", ".join(f"{e(k)}={v:g}" for k, v in list(a.values.items())[:3]) + "]" if a.values else "")
            for a in r.pine_alerts[:5]
        )
        lines += ["", f"<b>Pine alerts (6h)</b> {recent}"]

    lines += ["", f"Sources: {e(', '.join(r.data_sources) or 'n/a')} · TFs: {', '.join(t.value for t in r.timeframes_analyzed)}"]
    if r.errors:
        lines.append(f"⚠️ Partial data: {e('; '.join(r.errors)[:300])}")
    lines.append("<i>Not financial advice. Automated analysis for research only.</i>")
    return "\n".join(lines)


def build_application(
    settings: Settings,
    orchestrator: ScanOrchestrator,
    market: CompositeMarketDataProvider,
    alert_store: AlertStore,
    nansen: NansenClient,
    tv_session: TradingViewSessionClient | None = None,
    studies: StudyRegistry | None = None,
    advisor: StudyAdvisor | None = None,
) -> Application:
    allowed = settings.allowed_user_ids
    last_scan: dict[int, float] = {}

    def authorised(update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id in allowed

    async def _advise(script: ScriptInfo, inputs: dict[str, Any]) -> Advice | None:
        """Gather what the script says about itself, then let TypeSafe pick the bucket, plot and thresholds."""
        if advisor is None or tv_session is None:
            return None
        meta: dict[str, Any] | None = None
        study = None
        try:
            meta = await tv_session.translate_pine(script.pine_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pine metadata unavailable for advice", extra={"script": script.name, "error": str(exc)})
        probe_tf = parse_timeframe(settings.typesafe_probe_timeframe) or Timeframe.H4
        try:
            study = await tv_session.get_study(
                settings.typesafe_probe_symbol, probe_tf, script.pine_id, inputs, settings.typesafe_probe_bars
            )
        except Exception as exc:  # noqa: BLE001 - values sharpen the judgment but metadata alone is enough
            logger.info("study probe failed; advising from metadata only",
                        extra={"script": script.name, "symbol": settings.typesafe_probe_symbol, "error": str(exc)})
        if meta is None and study is None:
            return Advice(reason="Could not read the script from TradingView — kept manual defaults.")
        return await advisor.suggest(script, meta=meta, study=study)

    async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(_USAGE, parse_mode=ParseMode.HTML)

    async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return
        if not authorised(update):
            await msg.reply_text(f"⛔ Not authorised (your user id: {user.id}).")
            return
        args = context.args or []
        if not args:
            await msg.reply_text(_USAGE, parse_mode=ParseMode.HTML)
            return
        symbol = args[0]
        timeframe = Timeframe.H4
        if len(args) > 1:
            parsed = parse_timeframe(args[1])
            if parsed is None:
                await msg.reply_text(f"Invalid timeframe. Use one of: {SUPPORTED_LABEL}")
                return
            timeframe = parsed

        now = time.monotonic()
        if now - last_scan.get(user.id, 0.0) < settings.telegram_scan_cooldown_seconds:
            await msg.reply_text(f"⏳ Please wait {settings.telegram_scan_cooldown_seconds}s between scans.")
            return
        last_scan[user.id] = now

        status_msg = await msg.reply_text("🔍 Starting scan...")

        async def on_status(text: str) -> None:
            try:
                await status_msg.edit_text(text)
            except BadRequest:
                pass

        try:
            report = await orchestrator.scan(symbol, timeframe, on_status)
        except ScanError as exc:
            await status_msg.edit_text(f"❌ {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
            return
        except Exception:
            logger.exception("scan crashed", extra={"symbol": symbol, "user_id": user.id})
            await status_msg.edit_text("❌ Scan failed due to an internal error. Check server logs.")
            return

        try:
            await status_msg.edit_text(format_report(report), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except BadRequest as exc:
            logger.error("report render failed", extra={"error": str(exc)})
            await status_msg.edit_text(
                f"{report.symbol} {report.primary_timeframe.value}: {report.signal.value} score {report.score:.0f}/100"
            )

    async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not authorised(update):
            return
        health = await market.health()
        redis_ok = await alert_store.ping()
        exec_mode = "LIVE" if settings.execution_live else ("dry-run" if settings.execution_enabled else "disabled")
        lines = ["<b>System status</b>"] + [f"{html.escape(k)}: {html.escape(v)}" for k, v in health.items()]
        lines += [
            f"redis: {'ok' if redis_ok else 'down'}",
            f"nansen: {settings.nansen_mode} ({'key set' if nansen.enabled else 'no api key'})",
            f"typesafe: {'on' if advisor is not None and advisor.enabled else 'off'} (auto-configures /indicators add)",
            f"execution: {exec_mode}",
        ]
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_indicators(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not authorised(update):
            return
        if tv_session is None or studies is None or not tv_session.authenticated:
            await msg.reply_text("TradingView account session not configured. Set TV_SESSION_ID in .env (see README, Route B).")
            return
        args = context.args or []
        sub = args[0].lower() if args else "list"

        if sub == "list":
            try:
                scripts = await studies.refresh(tv_session)
            except Exception as exc:  # noqa: BLE001
                await msg.reply_text(f"❌ Could not list account scripts: {html.escape(str(exc))}")
                return
            active_ids = {spec.get("pine_id") for spec in studies.active().values()}
            lines = [f"<b>Scripts on your TradingView account</b> ({len(scripts)})"]
            for i, s in enumerate(scripts[:60], 1):
                star = "★ " if s.pine_id in active_ids else ""
                lines.append(f"{i:>2}. {star}{html.escape(s.name)} <i>[{html.escape(s.kind)}/{html.escape(s.source)}]</i>")
            if len(scripts) > 60:
                lines.append(f"… {len(scripts) - 60} more")
            lines.append("\nActivate with <code>/indicators add &lt;n&gt; plot=plot_0 above=0 below=0 points=5</code>")
            await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        if sub == "active":
            active = studies.active()
            if not active:
                await msg.reply_text("No account indicators active. Run /indicators to list and add.")
                return
            lines = ["<b>Active account indicators</b>"]
            for name, spec in active.items():
                rule = spec.get("rule") or {}
                lines.append(
                    f"• <b>{html.escape(name)}</b> ({html.escape(str(spec.get('display', '')))}) — bucket {rule.get('bucket')}, "
                    f"{rule.get('points')} pts, {rule.get('value')} &gt; {rule.get('bullish_above')} bull / &lt; {rule.get('bearish_below')} bear"
                    + (f", inputs {html.escape(str(spec.get('inputs')))}" if spec.get("inputs") else "")
                )
            await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        if sub == "add":
            if len(args) < 2:
                await msg.reply_text("Usage: /indicators add <n|pine_id> [plot=… above=… below=… points=… bucket=… age=… auto=off in.Input=value]")
                return
            script = studies.resolve(args[1])
            if script is None:
                await msg.reply_text("Unknown indicator. Run /indicators first, then use its number, or pass a pine id like USER;abc123.")
                return
            rule_opts, inputs = parse_kv(args[2:])

            # Auto-configuration only fills what the user left unspecified; any explicit flag wins.
            wants_auto = str(rule_opts.get("auto", "on")).lower() not in ("off", "false", "0", "no")
            manual = {"plot", "above", "below"} & rule_opts.keys()
            advice: Advice | None = None
            progress = None
            if wants_auto and not manual and advisor is not None and advisor.enabled:
                progress = await msg.reply_text("🤖 Reading the script's metadata and asking TypeSafe how to score it...")
                advice = await _advise(script, inputs)
                if advice is not None and advice.rule_opts is not None:
                    rule_opts = {**advice.rule_opts, **rule_opts}

            try:
                name, rule = studies.add(script, rule_opts, inputs, alias=str(rule_opts.get("as")) if "as" in rule_opts else None)
            except (ValueError, TypeError) as exc:
                text = f"❌ Invalid option: {html.escape(str(exc))}"
                await (progress.edit_text(text) if progress is not None else msg.reply_text(text))
                return

            lines = [
                f"✅ Activated <b>{html.escape(name)}</b> → {rule.bucket} bucket, {rule.points:g} pts, "
                f"{rule.value} &gt; {rule.bullish_above:g} bullish / &lt; {rule.bearish_below:g} bearish."
            ]
            if advice is not None:
                lines.append(f"🤖 {html.escape(advice.reason)}")
                lines += [f"• {html.escape(d)}" for d in advice.details]
            if not (advice is not None and advice.applied) and "plot" not in rule_opts:
                lines.append(
                    "Defaulted to plot_0 with 0 as the bull/bear threshold — adjust with plot=/above=/below= "
                    "if this is an oscillator around 50, etc."
                )
            lines.append("It will be pulled on every /scan.")
            text = "\n".join(lines)
            await (progress.edit_text(text, parse_mode=ParseMode.HTML) if progress is not None
                   else msg.reply_text(text, parse_mode=ParseMode.HTML))
            return

        if sub == "remove" and len(args) > 1:
            await msg.reply_text("Removed." if studies.remove(args[1]) else "Not found among active indicators.")
            return

        if sub == "clear":
            studies.clear()
            await msg.reply_text("Cleared all active account indicators.")
            return

        await msg.reply_text(_USAGE, parse_mode=ParseMode.HTML)

    application = Application.builder().token(settings.telegram_bot_token.get_secret_value()).build()
    application.add_handler(CommandHandler(["start", "help"], cmd_help))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("indicators", cmd_indicators))
    return application
