# User Guide — TradingView Confluence Scanner

Everything you need to operate, tune and extend the scanner. Deployed instance: `/opt/tradingview-scanner` on the VPS, Telegram bot, webhook at `https://tvwebhook.dedyn.io/webhooks/tradingview`.

Contents: [1 What it does](#1-what-it-does) · [2 Telegram commands](#2-telegram-commands) · [3 Reading a report](#3-reading-a-report) · [4 Scoring rules](#4-scoring-rules) · [5 Full workflow](#5-full-workflow) · [6 Settings reference](#6-settings-reference) · [7 How-tos](#7-how-tos) · [8 Backtesting](#8-backtesting) · [9 Operations](#9-operations) · [10 Troubleshooting](#10-troubleshooting) · [11 File map](#11-file-map)

---

## 1. What it does

You send `/scan <TICKER> [TIMEFRAME]` in Telegram. The agent:

1. Classifies the ticker (crypto vs stock).
2. Pulls candles for the requested timeframe **plus** 1h/4h/1d (and 1w for daily/weekly scans) from TradingView's chart feed, with Binance/Yahoo/Twelve Data fallbacks, and TradingView's technical rating snapshot.
3. Pulls any **account indicators** you activated (`/indicators`) and any **Pine alerts** received via webhook in the last 6 h.
4. If crypto and a Nansen key is set, pulls Smart-Money / exchange-flow / holder data (advisory).
5. Scores bull and bear evidence independently on a 0–100 matrix, applies regime vetoes, computes entry / stop / TP1-3 / RRR / position size.
6. Replies with a verdict: **BUY / SELL / WATCH / NEUTRAL** and the full breakdown.
7. Journals the result (`data/signals.jsonl`) and, only if execution is enabled and not dry-run, posts a signed payload to your execution webhook.

```
Telegram /scan ──► Orchestrator ──┬─► TradingView feed (candles, primary) ─┐
                                  ├─► Binance / Yahoo / Twelve Data (fallback)
                                  ├─► TradingView rating snapshot          ├─► indicators ─► Decision Engine ─► report
                                  ├─► Account studies (/indicators)        │                    │
                                  └─► Nansen (advisory, crypto)  ──────────┘                    └─► journal · execution webhook (gated)
Pine alerts ──► https://<domain>/webhooks/tradingview ──► alert store (Redis, 6 h) ─────────────┘
```

## 2. Telegram commands

Only user ids in `TELEGRAM_ALLOWED_USER_IDS` are accepted; anyone else gets "Not authorised (your user id: N)".

| Command | What it does |
|---|---|
| `/scan BTCUSDT` | Scan on the default 4h |
| `/scan AAPL 1d` · `/scan BINANCE:SOLUSDT 1h` · `/scan ETHUSDT 1w` | Scan a specific timeframe: `5m 15m 30m 1h 4h 1d 1w` |
| `/scan crypto:XYZ` · `/scan stock:LINK` | Force the asset class when the heuristic guesses wrong |
| `/status` | Upstream health: TradingView session (anonymous/authenticated), MCP, Binance, Twelve Data, Redis, Nansen mode, execution mode |
| `/indicators` | List scripts on your TradingView account (needs `TV_SESSION_ID`) |
| `/indicators add <n\|pine_id> [options]` | Activate one for every scan — see [7.3](#73-use-an-invite-only--private-indicator-from-your-account) |
| `/indicators active` · `remove <name>` · `clear` | Manage the active set |
| `/help` | Usage |

Rate limit: one scan per user per `TELEGRAM_SCAN_COOLDOWN_SECONDS` (10 s).

## 3. Reading a report

```
BTCUSDT · 4h · crypto — ⚪ NEUTRAL
Score 33/100 ▓▓▓░░░░░░░  (bull 33 / bear 7, coverage 90%)
```
- **Verdict**: 🟢 BUY / 🔴 SELL only when score ≥ 80, effective RRR ≥ 2.5 and **no vetoes**. 🟡 WATCH = 60–79. ⚪ NEUTRAL otherwise.
- **bull / bear**: both sides are scored; the higher one is the direction. A big gap means one-sided evidence.
- **coverage**: how many of the 100 points had data. 90 % = Nansen absent (normal without a key).

**Confluence Matrix** — points per bucket for the chosen direction (see §4).

**Levels** — entry = last close; SL = swing ± 1.5×ATR (or entry ± 2×ATR if no swing); TP1/2/3 = 1.5R / 2.5R / 4R; **RRR** = effective (capped by the nearest opposing swing — "structural target"); **Size** appears when `ACCOUNT_EQUITY` is set.

**Regime** — ADX (trending/chop), RSI, higher-timeframe bias.

**Confluence notes** — every piece of evidence that contributed (▲ bullish, ▼ bearish, • neutral/info).

**⛔ Vetoes** — hard blockers (RRR too low, stop too wide, counter-trend vs HTF, ADX chop, RSI over-extended, snapshot-only data, strict-Nansen flags). Any veto ⇒ no BUY/SELL.

**⚠️ Cautions** — advisory only (Nansen in advisory mode); they cost points but never block.

**Trade plan** — the management rules (scale-out at TP1/TP2, breakeven, time stop).

**On-chain / Relative strength / Pine alerts / Sources / Partial data** — what fed the score and what was missing.

## 4. Scoring rules

TradingView-derived evidence carries **90 points**; on-chain is an optional 10.

| Bucket | Max | Inputs |
|---|---|---|
| Trend & Structure | 30 | EMA 20/50/200 ribbon per TF (primary weighted 50 %, others 25 %); MSB / CHoCH on 1h & 4h |
| Momentum & Volatility | 30 | Hidden RSI divergence per TF; BB squeeze (BBW < 0.05) + volume > 1.5× 20-SMA |
| TradingView Indicators | 20 | TradingView `Recommend.All` rating per TF (≤ 10) + your custom indicators (alerts & account studies) via `config/custom_indicators.json` |
| Context — crypto | 10 | Nansen: SM 24h netflow (4), exchange netflow (4), top-10 holder change (2); `NANSEN_MODE` decides veto vs caution |
| Context — stock | 10 | Volume-profile position vs POC / value area (5); 20-bar return vs sector ETF or SPY (5) |
| Execution Risk | 10 | RRR at TP2 vs `MIN_RRR`; 0 if stop distance > `MAX_STOP_DISTANCE_PCT` |

Regime gates (vetoes): `HTF_BIAS_FILTER`, `MIN_ADX`, `RSI_OVEREXTENDED`, structural RRR cap, stop-distance cap, snapshot-only data.

## 5. Full workflow

**Every scan** (automatic): `/scan` → status edits (🔍 fetching → 📈 account indicators → 📊 Nansen/relative strength → 🧮 scoring → ✅) → report → journal → execution router (logs `disabled`/`dry_run`/`sent`).

**Your loop, day to day**
1. Keep TradingView alerts pointed at the webhook so your custom indicators are "fresh" (they only count within `max_age_bars` of their timeframe).
2. Scan the assets you care about on the timeframe you trade; treat WATCH as "set an alert on the structural level", BUY/SELL as "the checklist is satisfied — apply your own final judgement and the trade plan".
3. Once a week: `docker compose exec app python -m app.backtest --journal data/signals.jsonl` to see how live calls resolved; adjust thresholds only with evidence.
4. Change `.env` → `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` (restart picks up the change). Rules/study files are read at startup too, except the `/indicators` selection which is live.

## 6. Settings reference

All settings live in `/opt/tradingview-scanner/.env` (`chmod 600`). After editing: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`.

### Required
| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather. One poller per token — never run two bots with the same token |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated numeric ids. Empty = nobody can scan |
| `TV_WEBHOOK_SECRET` | Must equal the *Webhook secret* input in your Pine scripts |
| `WEBHOOK_DOMAIN`, `ACME_EMAIL` | Hostname for Caddy/Let's Encrypt (prod overlay) |

### TradingView
| Variable | Default | Meaning |
|---|---|---|
| `TV_FEED_ENABLED` | true | Chart websocket feed (primary candles). Anonymous works |
| `TV_SESSION_ID`, `TV_SESSION_ID_SIGN` | — | Browser cookies from a logged-in tradingview.com session. Unlock your plan's data and `/indicators`. Treat as a password |
| `TV_USERNAME`, `TV_PASSWORD` | — | Alternative login; usually blocked by captcha — prefer the cookie |
| `TV_STUDIES_PATH` | config/tv_studies.json | Seed list of account studies |
| `TV_STUDIES_ACTIVE_PATH` | data/tv_studies_active.json | Live selection managed by `/indicators` |
| `TV_MCP_URL`, `TV_MCP_OHLCV_TOOL`, `TV_MCP_SNAPSHOT_TOOL` | — | Optional MCP server (compose `--profile mcp`) |
| `TV_SCANNER_STOCK_MARKET`, `TV_SCANNER_DEFAULT_STOCK_EXCHANGES`, `TV_SCANNER_DEFAULT_CRYPTO_EXCHANGE` | america / NASDAQ,NYSE,AMEX / BINANCE | Symbol resolution for the rating snapshot |

### Webhook hardening
| Variable | Default | Meaning |
|---|---|---|
| `TV_WEBHOOK_ENFORCE_IP_ALLOWLIST` | true | Only TradingView's published IPs may POST |
| `TV_WEBHOOK_IP_ALLOWLIST` | TV's 4 IPs | Update if TradingView announces new ones |
| `TV_WEBHOOK_TRUST_PROXY` | false (prod overlay sets true) | Read `X-Forwarded-For` (needed behind Caddy) |
| `TV_WEBHOOK_HMAC_KEY` | — | Only for relays that can set `X-Signature`; leave empty for direct TradingView alerts |
| `TV_WEBHOOK_MAX_SKEW_SECONDS` | 300 | Reject alerts with stale timestamps |
| `TV_ALERT_TTL_SECONDS` | 21600 | How long alerts stay in the store |

### Data sources
| Variable | Meaning |
|---|---|
| `TWELVEDATA_API_KEY` | Keyed stock candles, used before Yahoo |
| `NANSEN_MODE` | `off` / `advisory` (default) / `strict` |
| `NANSEN_API_KEY`, `NANSEN_BASE_URL`, `NANSEN_CACHE_TTL_SECONDS` | Nansen REST v1; responses cached in Redis 5 min |
| `NANSEN_TOKEN_MAP_PATH` | Symbol → chain/contract map (`config/nansen_token_map.json`); unmapped tokens skip on-chain |
| `NANSEN_SM_NETFLOW_FULL_SCORE_USD` (1 M), `NANSEN_EXCHANGE_INFLOW_VETO_USD` (5 M) | Scaling / threshold for flow points and strict vetoes |
| `REDIS_URL` | Set by compose; in-memory fallback if unreachable |

### Decision engine
| Variable | Default | Effect |
|---|---|---|
| `MIN_SIGNAL_SCORE` | 80 | BUY/SELL threshold |
| `WATCH_SCORE` | 60 | WATCH threshold |
| `MIN_RRR` | 2.5 | Required effective RRR |
| `ATR_MULTIPLIER` | 1.5 | Stop distance beyond the swing |
| `MAX_STOP_DISTANCE_PCT` | 8 | Wider stops are vetoed (raise for 1d/1w trading) |
| `HTF_BIAS_FILTER` | true | No trades against the next-higher timeframe ribbon |
| `MIN_ADX` | 20 | Chop filter on the primary TF |
| `RSI_OVEREXTENDED` | 75 | No BUY above / SELL below (100 − value) |
| `ACCOUNT_EQUITY`, `RISK_PER_TRADE_PCT` | 0 / 1.0 | Position sizing (shown when equity > 0) |
| `CANDLE_LIMIT` | 300 | Bars per timeframe |
| `BENCHMARK_SYMBOL` | SPY | Fallback benchmark when sector is unknown |
| `CUSTOM_INDICATORS_PATH` | config/custom_indicators.json | Rules for custom indicators |
| `SIGNAL_JOURNAL_PATH` | data/signals.jsonl | Live signal log |
| `BACKTEST_FEE_BPS`, `BACKTEST_SLIPPAGE_BPS`, `BACKTEST_TIME_STOP_BARS` | 10 / 5 / 40 | Backtest costs; time stop also appears in the trade plan |

### Execution (off by default)
| Variable | Meaning |
|---|---|
| `EXECUTION_ENABLED` | Must be `true` to do anything at all |
| `DRY_RUN` | Must be `false` to actually POST; otherwise the signed payload is only logged |
| `EXECUTION_WEBHOOK_URL` | Your execution bridge (3Commas, exchange adapter, IBKR gateway…) |
| `EXECUTION_HMAC_KEY` | Signs `"<X-Timestamp>.<body>"` with HMAC-SHA256; unsigned sends are refused |

Payload: symbol, side, timeframe, entry, stop_loss, take_profits[3], score, rrr, position_units, management[], idempotency_key, generated_at, dry_run. Headers: `X-Timestamp`, `X-Signature`, `X-Idempotency-Key`.

## 7. How-tos

### 7.1 Send a TradingView alert into the scanner
1. Pine Editor → paste `pine_script_template.pine` (or your own script with an `alert()` call producing the same JSON) → Add to chart.
2. Script inputs: *Indicator name* (must match a key in `custom_indicators.json`, or a default rule applies) and *Webhook secret* = `grep TV_WEBHOOK_SECRET .env`.
3. Create alert → Condition: the script → "Any alert() function call" → Webhook URL `https://tvwebhook.dedyn.io/webhooks/tradingview`.
4. Confirm: `docker compose logs app | grep "pine alert accepted"`; the next `/scan` shows it under *Pine alerts*.

**Local test without TradingView** (from the VPS). Requests via the published port arrive from the Docker bridge IP, not 127.0.0.1, so the allowlist rejects plain `curl`. Because the prod overlay trusts proxy headers (Caddy overwrites them for real traffic), present a TradingView IP explicitly:
```bash
SECRET=$(grep ^TV_WEBHOOK_SECRET .env | cut -d= -f2)
curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8080/webhooks/tradingview \
  -H 'Content-Type: application/json' -H 'X-Forwarded-For: 52.89.214.238' \
  -d "{\"ticker\":\"BINANCE:BTCUSDT\",\"timeframe\":\"240\",\"indicator\":\"WebhookTest\",\"signal\":\"BUY\",\"price\":78000,\"timestamp\":$(date +%s000),\"secret_key\":\"$SECRET\"}"
curl -s http://127.0.0.1:8080/alerts/BTCUSDT -H "X-Webhook-Secret: $SECRET"
```
Expect `HTTP 202`, then `409` on a repeat (dedupe), `401` with a wrong secret.

Payload format (if writing your own): `{"ticker":"BINANCE:BTCUSDT","timeframe":"240","indicator":"Name","signal":"BUY|SELL|NEUTRAL","price":123.4,"timestamp":1726650000000,"values":{"k":1.2},"secret_key":"..."}`.

### 7.2 Stream any chart indicator's values (no source access needed)
Use `pine_custom_indicator_bridge.pine`: set *Source 1..3* to the plots of other indicators on the chart (`input.source`), name it, create the alert as above. It sends a `NEUTRAL` state alert every bar with `values{src1,src2,src3,rsi,atr}` and BUY/SELL on threshold crossings.

### 7.3 Use an invite-only / private indicator from your account
1. Log in to tradingview.com in a browser → DevTools → Application → Cookies → copy `sessionid` (and `sessionid_sign`).
2. `.env`: `TV_SESSION_ID=...`, `TV_SESSION_ID_SIGN=...` → `docker compose … up -d`.
3. `/status` should show `tradingview_session: authenticated`.
4. `/indicators` → numbered list. `/indicators add 3 plot=plot_0 above=0 below=0 points=6` activates #3.
   - `plot=` which plot to read (`plot_0`, `plot_1`, or the sanitised plot title); `above=` / `below=` thresholds; `points=` weight; `bucket=` indicators|trend|momentum|context; `age=` freshness in bars; `in.<InputTitle>=<value>` sets the script's inputs; `as=<alias>`.
5. `/indicators active` to review; every scan now pulls those studies on the primary timeframe.

This uses TradingView's internal chart protocol (unofficial). If TradingView changes it, `/indicators` fails cleanly and scans continue without the studies.

### 7.4 Weight your custom indicators
Edit `config/custom_indicators.json` and restart:
```json
"MyIndicator": {"bucket": "indicators", "points": 6, "max_age_bars": 3,
                "value": "osc", "bullish_above": 55, "bearish_below": 45, "opposite_penalty": 0.5}
```
Omit `value` to use the alert's BUY/SELL field instead. Bucket caps still apply.

### 7.5 Turn Nansen on/off/strict
`NANSEN_MODE=off|advisory|strict` + `NANSEN_API_KEY`. Advisory: on-chain contrary evidence lowers the score and lists cautions. Strict: negative SM netflow or exchange inflow ≥ veto USD blocks longs (mirror for shorts). Map new tokens in `config/nansen_token_map.json`.

### 7.6 Enable position sizing
`ACCOUNT_EQUITY=10000`, `RISK_PER_TRADE_PCT=1` → reports show units, notional and risk amount; execution payloads include `position_units`.

### 7.7 Enable live execution (only after forward-testing)
1. Build/verify your receiver: it must check `X-Signature = HMAC_SHA256(key, "<X-Timestamp>.<body>")`, reject stale timestamps, dedupe on `X-Idempotency-Key`.
2. `.env`: `EXECUTION_WEBHOOK_URL`, `EXECUTION_HMAC_KEY`, `EXECUTION_ENABLED=true`, keep `DRY_RUN=true` first → check logs show `execution dry-run` with the payload.
3. Only then `DRY_RUN=false`.

### 7.8 Rotate secrets
`TV_WEBHOOK_SECRET`: change in `.env` **and** in every Pine script's input. `TV_SESSION_ID`: log out/in on TradingView and paste the new cookie. Bot token: @BotFather `/revoke`.

## 8. Backtesting

```bash
docker compose exec app python -m app.backtest BTCUSDT --tf 4h --bars 1500
docker compose exec app python -m app.backtest AAPL --tf 1d --bars 800 --min-score 70 --exit-mode tp2
docker compose exec app python -m app.backtest --journal data/signals.jsonl     # forward-test live signals
```
Read the **score calibration** table first: hit-rate of +2.5R-before-−1R by score bucket should rise with score and the 80+ bucket should clear ~29 % (breakeven at 2.5R). Then trade metrics (`avg_r`, `profit_factor`, `max_drawdown_r`). Backtests run at 90 % coverage (no Nansen/alert history). Don't tune on fewer than ~30 trades; validate out-of-sample; use your real fee/slippage bps.

## 9. Operations

| Task | Command (run in `/opt/tradingview-scanner`) |
|---|---|
| Update code & redeploy | `sudo bash deploy/update.sh` |
| Apply `.env` change | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` |
| Logs | `docker compose logs -f app` · `docker compose logs caddy` |
| Health | `curl -s https://tvwebhook.dedyn.io/healthz` · `/status` in Telegram |
| Restart / stop | `docker compose restart app` · `docker compose down` |
| Backup | `tar czf ~/scanner-backup.tgz .env data` |
| Run tests (dev machine) | `.\.venv\Scripts\python.exe -m pytest -q` |
| Build image (dev machine, WSL) | `wsl -d Ubuntu -u root -- bash <scratch>/build_test.sh` |

Containers: `tv-scanner-app` (non-root, port bound to 127.0.0.1:8080), `tv-scanner-redis` (AOF, 128 MB cap), `tv-scanner-caddy` (80/443, auto-renewing certificate). Public paths: only `/webhooks/tradingview` and `/healthz`.

**Compose files:** Caddy is defined only in `docker-compose.prod.yml`, so `docker compose logs caddy` (or `up`, `ps`) fails with *"no such service"* unless both files are passed. Set once per shell — or add to `~/.bashrc` — and every `docker compose` command includes it:
```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
```

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Not authorised (your user id: N)" | Add N to `TELEGRAM_ALLOWED_USER_IDS`, restart |
| `telegram.error.Conflict … other getUpdates request` repeating | Another process uses the same bot token (`docker ps -a`); stop it or issue a new token |
| Report says "snapshot-only … no candles" | Every candle source failed for that TF; check `/status`, consider `TWELVEDATA_API_KEY`; signal is capped at WATCH |
| "Partial data: … rate limited (429)" | A fallback source (usually Yahoo) throttled; the scan still completed with the others |
| Webhook returns 403 | Sender IP not in TradingView's allowlist (or proxy header not trusted) |
| Webhook returns 401 | `secret_key` mismatch between Pine input and `.env` |
| Webhook returns 409 | Duplicate alert within 10 min (same ticker/tf/indicator/signal/timestamp) |
| `/indicators` → "session not configured" | `TV_SESSION_ID` missing or cookie expired |
| Caddy log shows ACME failures | DNS not resolving yet or ports 80/443 blocked at the provider; Caddy retries automatically |
| Score is high but verdict NEUTRAL/WATCH | Read **⛔ Vetoes** — usually structural RRR cap, stop-distance cap or HTF bias |

## 11. File map

```
app/main.py               FastAPI + Telegram lifecycle       app/decision_engine.py   scoring matrix, vetoes, levels
app/orchestrator.py       scan pipeline                      app/indicators.py        EMA/RSI/ADX/BB/ATR/structure/VP
app/telegram_bot.py       commands & report formatting       app/custom_indicators.py rules for custom indicators
app/tradingview_webhook.py  alert gateway                    app/study_registry.py    /indicators selection
app/execution_router.py   signed execution webhook           app/backtest.py          walk-forward backtester
app/clients/tradingview_ws.py  TV chart feed + studies       app/clients/*            Binance, Yahoo, Twelve Data, TV scanner, MCP, Nansen
config/custom_indicators.json  indicator weights             config/nansen_token_map.json  symbol → contract
config/tv_studies.json    seed account studies               data/                    journal + active studies (persisted)
deploy/bootstrap.sh, update.sh, Caddyfile                    docker-compose.yml (+ .prod.yml overlay)
pine_script_template.pine · pine_custom_indicator_bridge.pine
```
