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

The **base matrix is 100 points of TradingView-derived evidence**. On-chain data (Nansen) carries **zero base weight**: when it is activated and returns data it adds a *credit* of up to 10 points on top (score capped at 100). The stock-side context (volume profile / relative strength) is treated the same way.

| Bucket | Weight | Inputs |
|---|---|---|
| Trend & Structure | 30 | EMA 20/50/200 ribbon per TF (primary 50 %, others 25 %); MSB / CHoCH on 1h & 4h |
| Momentum & Volatility | 25 | Hidden RSI divergence per TF; BBW < 0.05 squeeze + volume > 1.5× 20-SMA |
| Institutional Flow | 20 | Anchored VWAP bias (5), liquidity sweep reversal (6), fair-value-gap proximity (4), order-block retest (3), premium/discount positioning (2) — see *Institutional playbook* |
| TradingView Indicators | 15 | TradingView's technical rating (`Recommend.All`) per TF (≤ 7.5) + your custom Pine indicators via alerts or account studies (default bucket for `custom_indicators.json`) |
| Execution Risk | 10 | SL = swing ± 1.5×ATR (or 0.5×ATR beyond swept liquidity); RRR at TP2 ≥ 2.5; stop distance ≤ 8 % |
| **Credit:** On-chain (crypto) | +10 | Only when `NANSEN_MODE` ≠ off **and** a key is set **and** data returns. `advisory` (default): credit + *cautions*; `strict`: credit + hard vetoes |
| **Credit:** Volume Profile & RS (stocks) | +10 | Price vs POC / value area; 20-bar return vs sector ETF (or SPY) |

Bull and bear are scored independently; the dominant side becomes the direction. A `BUY`/`SELL` is emitted only when score ≥ `MIN_SIGNAL_SCORE` (80), effective RRR ≥ `MIN_RRR` (2.5) and no veto fired; 60–79 → `WATCH`. Coverage counts base buckets only, so Nansen being off or absent never lowers it — a fully aligned TA setup reaches 100 on its own, and Nansen can only add.

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

### Institutional playbook (what "market maker / smart money" logic is actually in the code)

Real market making — quoting both sides, earning the spread, managing inventory with colocated infrastructure — is not a signal and cannot be replicated by a scanner. What *can* be measured from candles is the footprint large participants leave when they execute, and that is what the Institutional Flow bucket encodes (`app/institutional.py`):

| Footprint | Detection | How it's used |
|---|---|---|
| **Anchored VWAP** | VWAP from the most recent major swing (highest high / lowest low in 120 bars) with volume-weighted σ bands | Institutions benchmark fills to VWAP: price above a rising VWAP = buyers in control (5 pts); ±2σ bands shown as mean-reversion extremes |
| **Liquidity sweep** | A bar that trades beyond the prior swing low/high but closes back inside | Resting stops were taken and rejected — the classic stop-hunt reversal (6 pts). The stop is then placed 0.5×ATR beyond the swept level, which is where the liquidity *was*, usually tighter than swing − 1.5×ATR |
| **Fair value gap (imbalance)** | Three-candle gap between candle 1's high and candle 3's low (or inverse), not yet traded through | Unfilled imbalances get revisited; price at/near one in trend direction is an entry zone (4 pts), elsewhere 1 pt |
| **Order block** | Last opposing candle before an impulse ≥ 1.5×ATR that broke the candle's high/low, not invalidated | Retest of the zone institutions defended (3 pts near, 1 pt exists) |
| **Premium / discount** | Position inside the dealing range formed by recent swing highs/lows | Below 40 % = discount (favours longs, 2 pts); above 60 % = premium (favours shorts) |
| **Liquidity pools** | Clusters of equal highs / equal lows within 0.2×ATR | Used as the *structural target* when nearer than the last swing — price gravitates to resting orders |
| **Kill zones** | London 07–10 UTC, New York 12–15 UTC (`SESSION_FILTER`) | Intraday scans outside these windows get a caution, not a veto — participation, not direction |

Backtests include all of this automatically (no look-ahead: every detector uses bars ≤ the current one). Whether it adds edge on *your* instruments is an empirical question — read the calibration table.

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

## Asking in plain English (TypeSafe / Jev)

`/scan BTCUSDT 4h` is parsed in code and always will be — it is the fast path and never touches a model. Set `TYPESAFE_API_KEY` and everything that strict form has to *reject* gets a second chance:

```
/scan is btc worth a long on the 4h
🤖 Reading that as BTCUSDT 4h.
🔍 Starting scan...

what do you make of NVDA daily          ← no slash needed
🤖 Reading that as NVDA 1d.
```

`app/command_router.py` sends one request with four questions evaluated in parallel — the intent (`scan` / `status` / `indicators` / `help` / `other`), the timeframe, which listed cryptocurrency is meant, and which **word from your message** is the instrument — plus a yes/no on whether you named a company rather than a ticker. The argument questions are speculative: they are asked every time and ignored when the intent turns out not to be a scan.

The symbol is *selected, never generated*. The options are the words that actually appear in your message, so the model cannot introduce a ticker you did not type; spoken crypto names resolve through the same closed list `app/asset_classifier.py` already uses. Stocks are resolved by ticker only — ask about "apple" and it will tell you to use `AAPL` rather than guess.

| | |
|---|---|
| **When it runs** | Only when `/scan TICKER [TF]` does not parse, or on a plain message. Never inside a scan's scoring, never in the backtester. |
| **Timeframe** | Defaults to 4h when you do not say, or when the answer is below `TYPESAFE_MIN_CONFIDENCE`. |
| **When it declines** | Low confidence, a ticker that is not in your message, a company name with no ticker, or any API failure — all reply with the usage text instead of scanning something you did not ask for. |
| **Turning it off** | `TYPESAFE_NATURAL_LANGUAGE=false` or no key. The plain-message handler is not even registered, so the bot stays silent on non-commands. |

Only authorised users (`TELEGRAM_ALLOWED_USER_IDS`) are routed, and the usual `TELEGRAM_SCAN_COOLDOWN_SECONDS` applies to whatever the router decides to run.

### Tickers the static rules cannot place

`app/asset_classifier.py` decides crypto-vs-stock from an exchange prefix, a trailing quote asset (`USDT`, `BTC`, …), a `crypto:`/`stock:` prefix, or a hardcoded `KNOWN_CRYPTO_BASES` list — and when none of those fire it falls back to *stock*. That list goes stale with every new listing, so a bare `HYPE` typed the week it launched was scanned as an equity and returned nothing.

`classify()` now reports that guess as `ambiguous` instead of hiding it. It stays pure and synchronous — the webhook path and the backtester are untouched — and only `ScanOrchestrator` acts on the flag, asking TypeSafe crypto-or-stock once per ticker:

```
/scan HYPE
  → classify() → STOCK (ambiguous: no exchange, no quote asset, not in the known list)
  → resolver   → crypto @ 91%  →  re-classified as HYPEUSDT on Binance
```

| | |
|---|---|
| **When it runs** | Only on `/scan`, only when `ambiguous` is set. An exchange prefix, a quote asset, a `crypto:`/`stock:` prefix or a known base all skip it. |
| **Cost** | One request per ticker, cached in Redis for 30 days (in memory without Redis). `unclear` answers are cached too, so nothing is re-asked every scan. |
| **Threshold** | `TYPESAFE_SYMBOL_MIN_CONFIDENCE` defaults to 0.7 — stricter than the other two features, because this one redirects which market gets loaded. |
| **When it declines** | `stock`, `unclear`, low confidence, or any API failure all keep today's fallback, so the worst case is the behaviour you already have. |

A side benefit: Pine alerts arriving as `HYPEUSDT` are stored under that symbol, so resolving `/scan HYPE` to `HYPEUSDT` also makes those alerts match, which they previously did not.

### When an alert name does not match its rule

`config/custom_indicators.json` maps a Pine alert's `indicator` name to a bucket and a point weight, and the lookup is an exact (lowercased) key match. An alert calling itself `SuperTrend V2` against a config key of `SuperTrend_V2` misses, falls back to `DEFAULT_RULE` — indicators bucket, 5 points — and says nothing about it. You get a plausible-looking score built on a rule you did not write.

Two changes:

**Every scan now reports the mismatch**, with or without a TypeSafe key:

```
⚠️ Partial data: pine alert 'SuperTrend V2' matches no rule in custom_indicators.json — scored with defaults
```

**With `TYPESAFE_API_KEY` set, the name is matched back to your rule.** `app/indicator_matcher.py` asks one question per unmatched name — all in a single request — offering your configured rule names plus `none_of_these`, and describing what each rule does (bucket, points, which value it reads) so the choice is about the indicator rather than the spelling.

The matched rule is handed to the engine through the existing `ScanInputs.extra_rules`, keyed by the alert's own name, so `rule_for()` finds it on the next lookup. `DecisionEngine.evaluate()` is untouched and stays synchronous — which matters, because the backtester replays it bar by bar.

| | |
|---|---|
| **When it runs** | Once per scan, only for alert names with no exact rule. An exact match never reaches the model. |
| **Cost** | One request per scan regardless of how many names are unmatched (up to 8), cached per name for 30 days. The cache key includes a fingerprint of your rule set, so editing `custom_indicators.json` invalidates it. |
| **When it declines** | `none_of_these`, a rule name that does not exist, confidence below `TYPESAFE_INDICATOR_MIN_CONFIDENCE` (0.7), or any API failure — all leave the alert on `DEFAULT_RULE` and report it as unmatched. |

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

### Auto-configuring an indicator (TypeSafe / Jev)

Picking `plot=`, `above=` and `below=` by hand is the one thing `/indicators add` could never do for you: nothing in the payload says whether a script prints a MACD-style histogram around zero, an RSI-style 0–100 oscillator, or a SuperTrend line in dollars. Set `TYPESAFE_API_KEY` and `add` works it out from the script itself:

```
/indicators add 3                   # no flags needed
🤖 TypeSafe configured this automatically (86% confidence).
• bucket momentum (91%)
• zero_centred on Momentum (86%) → bullish > 0, bearish < 0
• 8 pts — strong evidence
```

`app/study_advisor.py` pulls the script's pine-facade metadata, probes ~120 bars of it on `TYPESAFE_PROBE_SYMBOL` to see what its plots actually print, and asks [TypeSafe](https://docs.typesafe.ai) four questions in one request: which confluence bucket it belongs to, which plot carries the directional signal, what kind of number that plot produces, and how much weight the evidence deserves. The model judges *semantics only* — code derives the actual thresholds from the observed values, so a plot the model called "0–100" that printed 64,000 is rejected rather than misconfigured.

It is deliberately confined:

| | |
|---|---|
| **When it runs** | Once, on `/indicators add`. Never during `/scan`, never in the backtester — scoring stays deterministic and reproducible. |
| **What overrides it** | Any flag you pass (`plot=`, `above=`, `points=`, `bucket=`…). `auto=off` skips it entirely; `TYPESAFE_AUTOCONFIG=false` or no key disables it. |
| **When it declines** | Confidence below `TYPESAFE_MIN_CONFIDENCE` (0.55), a price-overlay plot, an answer the observed values contradict, or any API failure. All of these fall back to today's `plot_0` / `0` / `0` defaults and say why. |

`/status` reports whether it is on.

Every request the four features make logs one line, whatever the outcome — including the answers that change nothing, which is what makes "was it even called?" answerable:

```json
{"msg": "typesafe call", "feature": "natural_language",  "outcome": "scan",   "ms": 96,  "confidence": 0.9, "resolved_symbol": "BTC"}
{"msg": "typesafe call", "feature": "symbol_resolution", "outcome": "stock",  "ms": 88,  "ticker": "AAPL", "said": "stock", "confidence": 0.95, "accepted": true}
{"msg": "typesafe call", "feature": "autoconfig",        "outcome": "declined", "ms": 121, "because": "TypeSafe was unsure (48% < 55%) ..."}
{"msg": "typesafe call", "feature": "natural_language",  "outcome": "error",  "ms": 34,  "error": "TypeSafeAuthenticationError: 401 ..."}
```

A cache hit deliberately logs nothing, so the absence of a line for a ticker you have scanned before is the cache working, not a failure. Errors also reach you in Telegram with the API's own message attached, rather than a bare "unavailable".

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

## Deploy on a VPS (Ubuntu 22.04/24.04 or Debian 12)

A 1 vCPU / 1 GB VPS is enough (app ≈ 250 MB RAM, Redis 128 MB cap, Caddy tiny).

**1. DNS for the webhook (free).** TradingView only calls HTTPS webhooks, so you need a hostname. At [duckdns.org](https://www.duckdns.org) create a subdomain (e.g. `mybot.duckdns.org`) and set its IP to the VPS. Skip this if you only want the Telegram scanner — then run without the prod overlay.

**2. Bootstrap the server** (installs Docker, opens ports 22/80/443, clones the repo into `/opt/tradingview-scanner`, writes `.env` with a random webhook secret):

```bash
ssh root@<vps-ip>
curl -fsSL https://raw.githubusercontent.com/pranmara/tradingview-scanner/main/deploy/bootstrap.sh | bash
```

**3. Configure**

```bash
nano /opt/tradingview-scanner/.env
```

Required: `TELEGRAM_BOT_TOKEN` (from @BotFather), `TELEGRAM_ALLOWED_USER_IDS`, `WEBHOOK_DOMAIN`, `ACME_EMAIL`. Optional: `TV_SESSION_ID` (account indicators), `NANSEN_API_KEY`, `TWELVEDATA_API_KEY`, `ACCOUNT_EQUITY`. Don't know your Telegram id yet? Start with it empty, send `/scan` to the bot, and it replies with your id.

**4. Start**

```bash
cd /opt/tradingview-scanner
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose logs -f app          # wait for {"msg": "service started", ...}
curl -s https://mybot.duckdns.org/healthz      # {"status":"ok"}  (Caddy fetches the certificate on first request)
```

Without a domain: `docker compose up -d --build` (base file only) runs bot + Redis; the webhook stays on `127.0.0.1:8080`.

**5. Verify from Telegram:** `/status` → upstream health; `/scan BTCUSDT 4h` → report.

**6. Point TradingView at it.** In the Pine alert dialog, Webhook URL = `https://mybot.duckdns.org/webhooks/tradingview`, and set the script's *Webhook secret* input to the `TV_WEBHOOK_SECRET` from `.env` (`grep TV_WEBHOOK_SECRET .env`).

**Operate**

| Task | Command |
|---|---|
| Update to latest code | `sudo bash /opt/tradingview-scanner/deploy/update.sh` |
| Logs | `docker compose logs -f app` (JSON lines; `docker compose logs caddy` for TLS/webhook access) |
| Restart | `docker compose restart app` |
| Backup | `tar czf scanner-backup.tgz /opt/tradingview-scanner/.env /opt/tradingview-scanner/data` |
| Backtest on the server | `docker compose exec app python -m app.backtest BTCUSDT --tf 4h --bars 1500` |
| See every TypeSafe request | `docker compose logs -f app \| grep '"msg": "typesafe call"'` |

Security notes: `.env` is `chmod 600`; the app port is bound to loopback and only `/webhooks/tradingview` + `/healthz` are proxied; the container runs as a non-root user; Caddy renews certificates automatically; keep `TV_WEBHOOK_ENFORCE_IP_ALLOWLIST=true` (Caddy passes the real client IP and the overlay sets `TV_WEBHOOK_TRUST_PROXY=true`).

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
