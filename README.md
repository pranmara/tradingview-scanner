# TradingView Scanner & Signal Validator

24/7 Telegram-driven chart scanner. `/scan BTCUSDT 4h` pulls 1h/4h/1d market data (TradingView MCP → Binance/Yahoo fallbacks), enriches crypto with Nansen smart-money flows, scores the setup on a 0–100 Confluence Matrix, computes ATR-based SL / TP1–3, and optionally forwards an HMAC-signed payload to an execution engine. A FastAPI gateway ingests Pine Script alerts that feed into scoring.

```
Telegram /scan ──► Orchestrator ──┬─► TradingView MCP ─┐
                                  ├─► Binance / Yahoo ─┼─► indicators ─► Decision Engine ─► Telegram report
                                  ├─► TV scanner REST ─┘        ▲              │
                                  └─► Nansen REST (crypto)      │              └─► Execution webhook (signed, gated)
Pine alert ──► POST /webhooks/tradingview ──► alert store ──────┘
```

## Confluence Matrix

TradingView-derived evidence carries **90 of 100 points**; on-chain data is an optional 10-point modifier.

| Bucket | Weight | Inputs |
|---|---|---|
| Trend & Structure | 30 | EMA 20/50/200 ribbon per TF (primary 50 %, others 25 %); MSB / CHoCH on 1h & 4h |
| Momentum & Volatility | 30 | Hidden RSI divergence per TF; BBW < 0.05 squeeze + volume > 1.5× 20-SMA |
| TradingView Indicators | 20 | TradingView's technical rating (`Recommend.All`) per TF (≤ 10) + your custom Pine indicators via alerts or account studies (default bucket for `custom_indicators.json`) |
| Context: On-chain (crypto) | 10 | Nansen, `NANSEN_MODE=off\|advisory\|strict`. Advisory (default) adds points and *cautions* only; strict turns negative SM netflow / heavy exchange inflow into hard vetoes |
| Context: Volume Profile & RS (stocks) | 10 | Price vs POC / value area; 20-bar return vs sector ETF (or SPY) |
| Execution Risk | 10 | SL = swing ± 1.5×ATR; RRR at TP2 ≥ 2.5; stop distance ≤ 8 % |

Bull and bear are scored independently; the dominant side becomes the direction. A `BUY`/`SELL` is emitted only when score ≥ `MIN_SIGNAL_SCORE` (80), effective RRR ≥ `MIN_RRR` (2.5) and no veto fired; 60–79 → `WATCH`. With Nansen absent or `off` the score is renormalised over the 90 TradingView points (coverage 90 %) — a fully aligned TA setup can trigger on its own.

**Effective RRR:** TP1/TP2/TP3 are 1.5R / 2.5R / 4R. If the nearest opposing swing (structural target) sits closer than 2.5R, RRR is capped at that structural value — a stop 1.5×ATR beyond the swing often makes this the binding constraint, which is intentional.

**Coverage:** if the on-chain bucket is unavailable (no Nansen key, unmapped token, API down) it is dropped and the score is renormalised over the remaining 70 points. The report shows `coverage 70%`.

## Trading thesis & best-practice gates

The matrix scores *evidence*; the gates below are hard vetoes that keep a high score from becoming a bad trade. They are standard practice among discretionary and systematic trend traders, and each is configurable in `.env`.

| Gate | Default | Why |
|---|---|---|
| Higher-timeframe bias (`HTF_BIAS_FILTER`) | on | No longs when the next-higher TF ribbon is bearish (and vice versa). Trading with the dominant trend is the single largest edge in trend-following studies; counter-trend confluence is usually a pullback in disguise. |
| ADX regime (`MIN_ADX=20`) | 20 | Ribbon alignment and MSB are trend tools. Below ADX 20 the market is ranging and breakouts mean-revert; the same signals lose their edge. |
| RSI over-extension (`RSI_OVEREXTENDED=75`) | 75 / 25 | Entering after a vertical move puts the stop far away and the first pullback often hits it. Wait for the reset. |
| Minimum RRR (`MIN_RRR=2.5`) with structural cap | 2.5 | At 2.5R you only need a 29 % hit-rate to break even. The nearest opposing swing caps RRR because price usually reacts there first. |
| Stop distance cap (`MAX_STOP_DISTANCE_PCT=8`) | 8 % | A wide stop means a tiny position or an oversized loss; either way the setup is not efficient. |
| Fixed-fractional sizing (`ACCOUNT_EQUITY`, `RISK_PER_TRADE_PCT=1`) | 1 % | Size = risk ÷ (entry − SL). Losing streaks of 8–10 are normal for a 40 % win-rate system; 1 % keeps a 10-loss streak at ~10 % drawdown. |
| Management plan | scale-out | 40 % at TP1 and SL → breakeven, 30 % at TP2 and SL → TP1, 30 % at TP3. Locks in R early while keeping a runner; also what the backtester simulates (`--exit-mode scaled`). |
| Time stop (`BACKTEST_TIME_STOP_BARS=40`) | 40 bars | A setup that hasn't moved in 40 bars has lost its catalyst; capital is better redeployed. |

Things that are deliberately **not** in the score: news, funding rates, order-book data, and sentiment. They are useful but need separate feeds; if you add them, treat them as vetoes first and points second.

**Honest expectations.** A confluence system like this typically produces few signals (a handful per symbol per month on 4h) with 35–50 % hit-rate at 2.5R. Expectancy comes from the asymmetry, not the win rate. Nothing here is a guarantee of profit — validate on your instruments with the backtester before risking capital.

## Backtesting: does the scanner produce what it claims?

`app/backtest.py` replays candles bar-by-bar through the **same** `indicators.py` + `decision_engine.py` used live. Signals are computed on bar *i* using only bars ≤ *i*; entries fill at the open of *i+1* with slippage; when a bar touches both stop and target, the stop is assumed first.

```bash
python -m app.backtest BTCUSDT --tf 4h --bars 1500               # ~250 days of 4h on Binance (paged)
python -m app.backtest ETHUSDT --tf 1h --bars 3000 --exit-mode tp2
python -m app.backtest AAPL --tf 1d --bars 800 --min-score 70    # stocks need TWELVEDATA_API_KEY for depth
python -m app.backtest BTCUSDT --tf 4h --bars 2000 --json out.json
```

What to read in the output:

1. **Score calibration** — the most important table. It bins *every evaluated bar* by score and shows how often price reached +2.5R before −1R within the time stop. If the scanner is doing its job the hit-rate rises monotonically with score, and the 80+ bucket clears the ~29 % breakeven with margin. If 60–80 performs as well as 80+, lower `MIN_SIGNAL_SCORE`; if 80+ is flat, the score isn't predictive on that instrument — don't trade it there.
2. **Trade metrics** — `avg_r` (expectancy per trade), `profit_factor` (>1.3 is respectable after costs), `max_drawdown_r` (size your account so this is survivable), `sharpe_per_trade`, `avg_bars_held`.
3. **Signal counts** — sanity-check frequency. Zero BUY/SELL over 1500 bars with many WATCH usually means the RRR gate or HTF filter is binding; run with `--min-score` to see what the unfiltered edge looks like.

Rules for trusting a backtest:

- **Coverage is 90 %** in backtests: Nansen and Pine alerts have no history, and TradingView ratings aren't available historically either (the Indicators bucket scores 0 unless you replay recorded alerts). Treat the backtest as validating the candle-derived core.
- **Out-of-sample**: tune thresholds on one period (e.g. `--bars 3000` of 1h), then re-run on a different symbol/period without changing anything. If the calibration table collapses, you overfit.
- **Sample size**: fewer than ~30 trades tells you nothing; use `--min-score` or more bars to get statistical mass, then apply the strict thresholds.
- **Costs matter**: `--fee-bps 10 --slippage-bps 5` is a taker on a major exchange; use your real numbers. Illiquid alts need 20–50 bps.
- **Forward-test before live**: every live BUY/SELL/WATCH is appended to `data/signals.jsonl`. After a few weeks run `python -m app.backtest --journal data/signals.jsonl` — it fetches the candles that arrived after each signal and reports realised R. Live results diverging from backtest is the earliest warning that a data source or filter is misbehaving.

## Connecting your TradingView account

TradingView has no official API for chart or indicator data, so there are two routes:

**Route A — alerts from your logged-in account (supported, recommended).** Every indicator you can put on a chart — including invite-only, paid and your own private scripts — can fire alerts, and `pine_custom_indicator_bridge.pine` can read their plots through `input.source()` without touching their source. This is how the scanner uses account-only indicators today. Limits: TradingView caps active alerts per plan, and alerts must be created per symbol/timeframe.

**Route B — session feed (unofficial, experimental).** `app/clients/tradingview_ws.py` speaks the websocket protocol TradingView's own web app uses. With your `sessionid` cookie it:
- pulls the exact candles your chart shows for any symbol your plan can see (`tv-session` becomes the first OHLCV source, ahead of MCP/Binance/Yahoo), and
- runs a Pine study by id server-side and returns its plot values. Entries in `config/tv_studies.json` are fetched on every scan and injected as `NEUTRAL` alerts with `values`, so they're scored by `config/custom_indicators.json` like any other indicator.

```bash
# 1. Copy the sessionid / sessionid_sign cookies from a logged-in browser into .env
# 2. Probe it (candles first, then a study)
python -m app.clients.tradingview_ws BINANCE:BTCUSDT 240
python -m app.clients.tradingview_ws BINANCE:BTCUSDT 240 --study "USER;0123456789abcdef" --input Length=20
```

Selecting indicators from Telegram (once `TV_SESSION_ID` is set):

```
/indicators                         # numbered list of scripts on your account (own, favourites, invite-only)
/indicators add 3 plot=plot_0 above=0 below=0 points=6      # activate #3; bullish when plot_0 > 0, bearish when < 0
/indicators add USER;abc123 plot=Signal above=55 below=45 in.Length=20 as=MyOsc
/indicators active                  # what /scan will pull
/indicators remove MyOsc            # or /indicators clear
```

The selection persists in `data/tv_studies_active.json` (seeded from `config/tv_studies.json`). Every `/scan` fetches each active study on the primary timeframe and scores it through the same rule engine as webhook alerts; the report lists them under *Pine alerts* and `tv-session-studies` in *Sources*. Pine ids can also be read from a script's URL (`PUB;…`, `USER;…`) or the `pine-facade/translate/...` request in the browser network tab. Study inputs are matched by their Pine `input()` title (`in.Length=20`).

Caveats: this is not an API TradingView offers; it can stop working after a TradingView release, the session cookie is a full-access credential (rotate it, never commit it), and heavy use may get the account rate-limited. Route A is the one to build on; Route B is for indicators whose alerts can't express the values you need.

## Custom TradingView indicators

Two ways to feed indicators the scanner can't compute itself:

**1. Signal alerts** — your Pine script calls `alert()` with `"signal":"BUY"|"SELL"` (see `pine_script_template.pine`). By default a fresh alert adds 5 points to the TradingView Indicators bucket on its side and subtracts 2.5 from the other.

**2. Value streams via the bridge** — `pine_custom_indicator_bridge.pine` uses `input.source()` so you can point it at *any plot of any indicator on the chart* (no source-code access needed). It sends a `NEUTRAL` state alert every bar close with `"values":{"src1":…,"src2":…,"src3":…,"rsi":…,"atr":…}`, plus BUY/SELL when `src1` crosses configurable levels.

`config/custom_indicators.json` decides what each indicator is worth:

```json
{
  "SuperTrend_V2":  {"bucket": "trend",    "points": 6, "max_age_bars": 3},
  "SqueezeMomentum":{"bucket": "momentum", "points": 5, "value": "mom", "bullish_above": 0, "bearish_below": 0},
  "Bridge":         {"bucket": "momentum", "points": 4, "value": "src1", "bullish_above": 0, "bearish_below": 0}
}
```

- `bucket`: `indicators` (default) / `trend` / `momentum` / `context` (context = the Nansen or Volume-Profile bucket; contributions are skipped if that bucket is unavailable).
- `points`: added to the indicated side; `opposite_penalty` (default 0.5) × points is subtracted from the other side. Bucket caps still apply, so custom indicators can't push a bucket past its weight.
- `value` mode: reads `alert.values[<key>]` and compares against `bullish_above` / `bearish_below` instead of trusting the BUY/SELL field. Use this for oscillators.
- `max_age_bars`: how many bars of the alert's own timeframe it stays valid.
- Only the **latest** alert per (indicator, timeframe) counts, so a state stream naturally supersedes itself.

Check what's arriving: `GET /indicators/BTCUSDT` with header `X-Webhook-Secret` returns the latest values per indicator. Rules are read at startup; restart the container after editing the file.

## Deploy on a VPS

```bash
git clone <repo> tradingview-scanner && cd tradingview-scanner
cp .env.example .env            # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS, TV_WEBHOOK_SECRET
docker compose up -d --build    # app + redis
docker compose logs -f app
curl -s localhost:8080/healthz  # {"status":"ok"}
```

- Telegram uses long polling — no inbound port needed for the bot. Only `8080` (webhook gateway) must be reachable by TradingView. Put it behind a TLS reverse proxy (Caddy/nginx) and set `TV_WEBHOOK_TRUST_PROXY=true` so the IP allowlist sees the real client IP.
- Find your Telegram user id by sending `/scan` once; the denial message prints it.
- Timeframes: `5m 15m 30m 1h 4h 1d 1w`. Every scan also pulls 1h/4h/1d for confluence, plus 1w when scanning 1d or 1w.
- Optional MCP container: `docker compose --profile mcp up -d --build` and set `TV_MCP_URL=http://tradingview-mcp:8000/mcp`.

## TradingView alerts

1. Paste `pine_script_template.pine` into the Pine Editor, set the **Webhook secret** input to `TV_WEBHOOK_SECRET`.
2. Create alert → *Any alert() function call* → Webhook URL `https://<host>/webhooks/tradingview`.
3. Alerts are stored 6 h (`TV_ALERT_TTL_SECONDS`) and count toward the Momentum bucket if received within 2 bars of their timeframe.

Webhook hardening (`app/tradingview_webhook.py`): TradingView IP allowlist → body size cap → optional HMAC header → constant-time `secret_key` compare → ±5 min timestamp skew → Redis dedupe (10 min).

## Execution webhook

Off by default. Both `EXECUTION_ENABLED=true` **and** `DRY_RUN=false` must be set, plus `EXECUTION_WEBHOOK_URL` and `EXECUTION_HMAC_KEY`. Until then the signed payload is logged with `dry_run=true` and nothing is sent.

Payload is signed as `HMAC-SHA256(key, "<X-Timestamp>.<body>")`, sent with headers `X-Timestamp`, `X-Signature`, `X-Idempotency-Key`. The receiver (3Commas bridge, Bybit adapter, IBKR gateway…) should verify the signature, reject stale timestamps, and dedupe on the idempotency key.

## Things to know before trusting it

- **TradingView cannot send custom HTTP headers.** `TV_WEBHOOK_HMAC_KEY` therefore only applies when a relay/proxy in front of the gateway adds `X-Signature`. For direct TradingView → gateway traffic the protection is the body secret + IP allowlist + timestamp/dedupe. Leave `TV_WEBHOOK_HMAC_KEY` empty in that case or every direct alert will be rejected.
- **Stock candles without a key are best-effort.** Yahoo's chart API rate-limits unauthenticated clients aggressively (the app bootstraps its cookie/crumb and serialises requests, but a 429 from Yahoo is still common on shared IPs). Set `TWELVEDATA_API_KEY` (free tier) for a reliable stock feed. If every candle source fails but the TradingView scanner snapshot works, the scan **degrades to snapshot-only**: EMA ribbon, RSI, BBW and ATR are used, structure/divergence/volume profile are skipped, and the signal is capped at `WATCH` with an explicit veto. Crypto is unaffected (Binance klines are keyless and reliable).
- **Yahoo 4h bars are resampled from 1h session bars** (4 consecutive bars within a UTC day). For US equities this is an approximation of TradingView's 4h candles; use the MCP source if you need exact bars. Crypto uses native Binance 4h klines.
- **Nansen response field names are matched tolerantly** (`app/clients/nansen.py`: `_SM_KEYS`, `_EXCHANGE_KEYS`, `_SHARE_KEYS`, `_CHANGE_KEYS`). Verify against your plan's API docs and extend the key lists if a metric shows as unavailable. Sign convention assumed: positive exchange netflow = inflow to exchanges.
- **`config/nansen_token_map.json`** maps symbol bases to chain + contract. `BTC` maps to WBTC on Ethereum as an on-chain proxy; `SOL` to wSOL. Unmapped tokens skip the on-chain bucket (coverage 70 %).
- **Community TradingView MCP servers expose different tool names.** Set `TV_MCP_OHLCV_TOOL` / `TV_MCP_SNAPSHOT_TOOL` to match; the client normalises list-of-dicts, list-of-lists and dict-of-arrays candle shapes. `/status` in Telegram lists the tools the server advertises. The whole stack works with `TV_MCP_URL` empty.
- **Sector relative strength** uses TradingView's `sector` field (via the scanner endpoint) mapped to SPDR sector ETFs in `app/orchestrator.py::SECTOR_ETF`; unknown sectors fall back to `BENCHMARK_SYMBOL` (SPY).
- **Holder concentration:** rising top-10 concentration is scored as accumulation (bullish), falling as distribution. Flip the sign in `_onchain_bucket` if your thesis differs.
- Public Binance / Yahoo / TradingView scanner endpoints are unauthenticated and rate-limited; retries use exponential backoff with jitter (`app/resilience.py`). Heavy use should go through the MCP server or a paid feed.

## Development

```bash
python -m venv .venv && .venv\Scripts\activate      # or source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload --port 8080           # needs a .env
```

Manual webhook test (allowlist disabled: `TV_WEBHOOK_ENFORCE_IP_ALLOWLIST=false`):

```bash
curl -X POST localhost:8080/webhooks/tradingview -H 'Content-Type: application/json' \
  -d '{"ticker":"BINANCE:BTCUSDT","timeframe":"240","indicator":"SuperTrend_V2","signal":"BUY","price":65000,"secret_key":"<TV_WEBHOOK_SECRET>"}'
```

Not financial advice. Automated analysis for research only.
