# Calibration: does the Confluence Matrix predict anything?

Measured 2026-09-20/21 against the live engine. Every number here came from `app/backtest.py` replaying the
same `DecisionEngine` the scanner uses, on Binance candles. Reproduction commands are at the bottom.

**Headline: the score does not rank setups, and no configuration tested was profitable out of sample.**
The data layer, the levels, the institutional detection and the alerting all work. The score-to-signal
mapping is the part that does not survive measurement.

---

## 1. A scoring bug found first

`_indicators_bucket` scored 0 in every backtest — backtests never populate `snapshot` — but kept its 15
points in the denominator, unlike `_institutional_bucket` and `_onchain_bucket` which drop out when they
have no data. Every backtest score was therefore ~15% below what the same bar would score live, making the
backtester useless for calibrating the live thresholds it exists to calibrate.

Fixed: the bucket is available when it has a TradingView rating **or** a Pine reading.

| BTCUSDT 4h, 989 bars | WATCH | max score | 60-80 bucket |
|---|---|---|---|
| before | 2 | 61.9 | n=2, hit 0.0% |
| after | 37 | 72.8 | n=37, hit 16.2% |

---

## 2. The score does not rank a setup's own outcome

AUC of each predictor against "reaches +2.5R before −1R", 13,924 bars over 5 markets. 0.50 is a coin flip.

| predictor | 2.5R/40 | 1.5R/40 | 2.5R/100 | 1.5R/100 |
|---|---|---|---|---|
| **score** | **0.494** | 0.485 | 0.483 | 0.483 |
| trend | 0.451 | 0.459 | 0.457 | 0.468 |
| momentum | 0.499 | 0.499 | 0.495 | 0.496 |
| institutional | 0.534 | 0.502 | 0.516 | 0.491 |
| execution | 0.587 | 0.565 | 0.565 | 0.545 |

Three things to take from this:

- **The composite is slightly *worse* than a coin flip.** `trend` + `momentum` are 55 of the 100 points —
  one mildly inverse, one noise — diluting the only informative component down to 10%.
- **`trend` is inversely related.** By the time the ribbon aligns on 1h, 4h and 1d, the move is extended.
  The bucket carrying the largest weight points the wrong way.
- **`execution` is the only positive, and it is nearly definitional.** It is a monotone function of
  `min(2.5, structural RRR)`, so "is there room for 2.5R" predicting "reaches 2.5R" is close to a tautology.
  Its decile table shows the whole effect is the *bottom* decile (1.4% hit rate) — it identifies hopeless
  setups, which the `effective_rrr >= min_rrr` gate already does.

Loosening the target to 1.5R and doubling the time stop lifts the base rate from 12.6% to 32.0% but leaves
the score's AUC at 0.48. **More winners, same inability to tell which.**

---

## 3. Real expectancy is negative

Trade simulator with scaled exits, 10bps fees and 5bps slippage, 4 markets.

| min_score | trades | win% | avgR | 95% CI on avgR | totalR |
|---|---|---|---|---|---|
| 0 | 225 | 40.0% | **−0.180** | −0.328 to −0.031 | −40.5 |
| 55 | 80 | 36.2% | −0.117 | −0.358 to +0.124 | −9.4 |
| 65 | 30 | 43.3% | +0.148 | −0.306 to +0.602 | +4.4 |
| 75 | 8 | 25.0% | −0.653 | −1.153 to −0.153 | −5.2 |

`min_score=0` takes everything the hard gates allow, and its interval excludes zero: **the gates alone lose
money.** The +0.148 at 65 rests on 30 trades with an interval spanning −0.31 to +0.60.

### Costs are a large share of the loss

```
cost in R = (2 × fee_bps + slippage_bps) / 10,000 ÷ stop_distance
          = 0.0025 ÷ stop_distance
```

| stop distance | cost per round trip |
|---|---|
| 1.2% (a real 1h scan) | **0.205R** |
| 2.0% (typical 4h) | 0.125R |
| 4.0% (typical 1d) | 0.063R |

This explains the timeframe split, the largest single effect measured: BTCUSDT 1h at `min_score=0` returned
−0.472R per trade against −0.077R averaged over three 4h markets. **Do not run this on 1h** — friction alone
is prohibitive at those stop distances, independent of signal quality.

---

## 4. Pre-registered sweep: one config passed training and failed out of sample

Rules fixed before any result was seen: train on the earlier 60% of each market, verify on the last 40%;
accept only a config whose train CI excludes zero, with ≥100 train trades, positive on ≥5 of 8 markets, and
a positive holdout.

| tf | atr | exit | train n | train avgR | CI-lo | mkts+ | holdout n | holdout avgR |
|---|---|---|---|---|---|---|---|---|
| 1d | 1.5 | scaled | 180 | −0.201 | −0.361 | 2/8 | 153 | +0.034 |
| 1d | 1.5 | tp2 | 175 | −0.007 | −0.244 | 5/8 | 152 | +0.242 |
| 1d | 2.5 | scaled | 179 | −0.200 | −0.361 | 2/8 | 151 | −0.002 |
| 1d | 2.5 | tp2 | 174 | −0.006 | −0.244 | 5/8 | 149 | +0.205 |
| 4h | 1.5 | scaled | 231 | +0.030 | −0.132 | 5/8 | 182 | −0.166 |
| **4h** | **1.5** | **tp2** | **229** | **+0.224** | **+0.024** | **6/8** | **178** | **−0.133** |
| 4h | 2.5 | scaled | 215 | −0.097 | −0.259 | 3/8 | 164 | −0.101 |
| 4h | 2.5 | tp2 | 212 | +0.084 | −0.122 | 6/8 | 160 | −0.052 |

**No configuration satisfied all four criteria.** One passed training at +0.224R per trade with a CI
excluding zero, positive on 6 of 8 markets, over 229 trades — a +51R backtest — and produced −0.133R out of
sample. Without the holdout rule written down beforehand, that cell is exactly what would have been shipped.

The 1d configs drifted positive in the holdout while the 4h configs drifted negative. Opposite drifts in the
two halves is regime dependence, not edge.

### The one finding that survived: exit flat at TP2

A single exit at TP2 beat the 40/30/30 scale-out in **8 of 8 comparisons**, in-sample and out. Both modes
take identical entries — the take condition does not depend on exit mode — so this is a clean
management-only comparison.

| config | scaled → tp2 |
|---|---|
| 1d atr 1.5 | −0.201 → −0.007 |
| 1d atr 2.5 | −0.200 → −0.006 |
| 4h atr 1.5 | +0.030 → +0.224 |
| 4h atr 2.5 | −0.097 → +0.084 |

Mechanism: scaling out moves the stop to breakeven after TP1, converting trades that would have reached TP2
into zeros. Worth roughly 0.2R per trade. `management_plan()` and the backtester default now reflect this.
It makes the system *less bad*, not profitable.

**Wider stops did not help.** ATR 2.5 was worse than 1.5 in 3 of 4 comparisons: cutting cost-per-R also
shrinks the RRR and changes which trades clear the `effective_rrr >= 2.5` gate, and the second effect wins.

---

## 5. Cross-sectional ranking also fails

A different question: not "is this chart good" but "which of these 51 is best right now". Ranking cancels the
market-wide move, so a predictor can be useless in isolation and still rank well. Information Coefficient is
the Spearman correlation between predictor and forward return at each non-overlapping rebalance date.

**This test initially produced a false positive from a bug in the harness.** Sampling every H-th bar *per
symbol* put each symbol on its own date grid, because they list on different dates; cross-sections were thin
and arbitrarily selected. Anchoring the stride to the calendar fixed it:

| | score IC | t | mom20 (control) IC | t | symbols/date |
|---|---|---|---|---|---|
| broken grid, 12 symbols | +0.058 | **2.69** | +0.018 | 0.77 | 8.0 |
| fixed grid, 12 symbols | +0.031 | 1.70 | +0.035 | 1.67 | 11.5 |
| fixed grid, 51 symbols | +0.005 | **0.42** | −0.002 | −0.17 | 36.8 |

The signal collapsed monotonically as the test became more correct and more powerful. On the corrected
12-symbol grid the score (t = 1.70) is indistinguishable from plain 20-bar momentum (t = 1.67), and neither
is significant.

At 51 symbols, 288 rebalances, nothing ranks:

| predictor | mean IC | t(IC) | spread% | t(spread) |
|---|---|---|---|---|
| score | +0.0052 | 0.42 | +0.913 | 1.14 |
| trend | −0.0013 | −0.10 | +1.070 | 1.31 |
| momentum | −0.0032 | −0.31 | −0.808 | −1.04 |
| institutional | +0.0060 | 0.53 | −0.326 | −0.60 |
| execution | −0.0096 | −0.92 | −0.613 | −1.24 |
| mom20 (control) | −0.0024 | −0.17 | +1.259 | 1.28 |

The only repeating signal is momentum's long/short **spread** — t = 2.81 at a 14-day horizon, t = 2.93 on the
12-symbol set. That is the classic momentum factor living in the tails, and it is not this score. With 36
tests the Bonferroni bar is |t| ≈ 3.2, so treat it as a lead.

---

## 6. H1 — funding extremes: a real signal you cannot trade

The first test of a *positioning* signal rather than a price pattern. Pre-registered before any funding data
was fetched: perps whose longs pay the most funding should deliver lower forward returns, net of funding, than
perps with the lowest funding. 87 Binance USDT perpetuals, 286 weekly rebalances on a calendar grid, a median
of 50 names per date, 2021-03 to 2026-09. Long the lowest-funding decile, short the highest, 20 bps cost per
rebalance.

| | mean IC | t(IC) | gross spread | net spread | t(net) |
|---|---|---|---|---|---|
| TRAIN 2021-03 .. 2024-06 | −0.0594 | **−4.01** | +0.521% | +0.321% | 0.43 |
| HOLDOUT 2024-07 .. 2026-09 | −0.0197 | −1.56 | −0.559% | −0.759% | −0.91 |

**Not accepted** (A1, A2 pass; A3, A4 fail). The 3-day horizon gives the same verdict.

This is the only effect in the whole investigation with the predicted sign **and** strong significance
(t = −4.01). Funding does carry real information about positioning. It fails for two reasons that are worth
more than the verdict.

### Carry is real, and momentum takes it back

| component (whole period, per weekly rebalance) | spread | t |
|---|---|---|
| funding carry collected | +0.782% | **14.36** |
| price move | −0.695% | −1.24 |
| **total** | **+0.087%** | 0.15 |

Shorting high-funding perps collects funding very reliably (t = 14). But perps have high funding *because* they
are rallying while longs crowd in, and those rallies tend to continue, so the price loss on the short leg
cancels the carry almost exactly. At the 3-day horizon it is +0.387% carry against −0.395% price. The market
prices the carry roughly fairly: it is payment for taking on momentum risk, not free money.

### The effect decayed

| year | IC | t | net spread |
|---|---|---|---|
| 2021 | −0.0961 | −3.02 | +3.781% |
| 2022 | −0.0640 | −2.47 | +1.211% |
| 2023 | −0.0690 | −2.39 | −1.567% |
| 2024 | −0.0009 | −0.05 | −1.873% |
| 2025 | −0.0107 | −0.55 | −0.171% |
| 2026 | −0.0258 | −1.18 | −1.693% |

It worked in 2021–22 and has been close to zero since 2024, which is consistent with more participants
running funding and basis trades. The train/holdout boundary (Jul 2024) falls right at the decay. Without the
holdout, a train IC of t = −4.01 would have read as a textbook factor.

### What not to do next

The obvious refinement — short high funding only once momentum stalls — is a *new* hypothesis designed after
seeing this data, including the holdout. It cannot be validated on the same data. If pursued, pre-register it
and test it only on data that arrives afterwards.

---

## 7. H2 — short build-up: nothing

Pre-registered before any open-interest data was fetched: when open interest rises while price falls, shorts
are piling in, and those perps should outperform as they get squeezed. The signal is
`S = z(ΔOI) − z(ΔP)`, measured on Bybit linear open interest in contract units, so price moves cannot change
it mechanically. 82 perps, 233 weekly rebalances, a median of 56 names per date, 2022-03 to 2026-09.

The signal contains price — "price fell" is half of it — so it was only accepted if it beat a price-only
reversal control (A5).

| | signal | mean IC | t(IC) | net spread | t(net) |
|---|---|---|---|---|---|
| TRAIN | S | −0.0023 | −0.16 | −0.716% | −0.83 |
| TRAIN | price-only control | +0.0218 | 1.22 | −0.209% | −0.23 |
| HOLDOUT | S | −0.0181 | −1.05 | −0.749% | −0.65 |
| HOLDOUT | price-only control | −0.0194 | −0.93 | −2.396% | −1.92 |

**Not accepted. All five criteria failed**, including A5: open interest added nothing beyond price. The
by-year IC changes sign from year to year with no pattern.

On its own, ΔOI shows IC −0.024 (t = −2.32): rising open interest goes with slightly *lower* returns, the
opposite of the squeeze story and consistent with H1's finding that crowded positioning underperforms. It was
a secondary readout measured over the whole period with no holdout, and its tradeable spread is t = 0.29, so
treat it as a curiosity.

Caveat: the open-interest window starts in 2022, so it misses 2021, H1's strongest year. But 2022 was H1's
second strongest year, and S showed nothing there either (IC −0.028).

---

## 8. The one reliable number, and why it isn't an edge either

H1's carry was collected at t = 14; it lost only because *price* moved against a long/short book built from
*different* coins. Holding spot and shorting the perp on the *same* coin cancels the price exposure and leaves
the funding. That is the well-known cash-and-carry trade. Funding a short-perp holder received, annualised:

| year | BTC + ETH | % of prints negative | all 87 perps |
|---|---|---|---|
| 2019 | 7.8% | 15% | 7.0% |
| 2020 | 22.3% | 8% | 15.7% |
| 2021 | 34.1% | 6% | 35.2% |
| 2022 | 2.5% | 28% | −1.9% |
| 2023 | 8.1% | 10% | 6.7% |
| 2024 | 12.4% | 6% | 9.1% |
| 2025 | 5.0% | 15% | −1.6% |
| 2026 | 2.3% | 29% | −4.7% |

**This is descriptive, not a backtest.** It is gross of spot trading fees, hedge rebalancing, margin, the
capital tied up in spot, and exchange counterparty risk. It measures how big the pool is.

It is a yield, not a trading edge: the trade gets paid for supplying leverage to longs. It is heavily
dependent on regime — 34% in the 2021 mania, 2.5% in 2022 — and it has compressed the same way H1 decayed. In
2026 BTC + ETH paid 2.3% gross, with 29% of prints negative, and across the wider perp universe it turned
negative in 2025. At current levels, compare it with what cash earns risk-free before taking exchange risk
to collect it.

---

## Caveats that apply to all of the above

- **Crypto only.** Yahoo rate-limited every equity attempt, so no stock is in any sample.
- **~2 to 5.5 years, one broad regime.** The train/holdout drift shows how much that matters.
- **Survivorship bias** in the 60-symbol universe: it is today's top names by volume, which biases *toward*
  finding an edge. A negative result is therefore strong; a positive one needs discounting.
- **`hit` is not P&L.** The AUC sections treat "did not reach target in N bars" as a loss, while the real
  simulator time-stops at market. Section 3 is the P&L-relevant one.
- **Non-overlapping windows throughout.** Overlapping them would have tripled the apparent sample and
  inflated every t-statistic.

## What to do with this

1. **Keep `EXECUTION_ENABLED=false`.** Now evidenced, not precautionary.
2. **Do not trade 1h.** Friction alone is disqualifying.
3. **Do not tune thresholds.** There is nothing to tune — §4 shows what tuning produces.
4. **Use the scanner as a scanner.** The multi-source aggregation, levels, footprints and alerting are sound.
5. **If pursuing an edge, change the evidence, not the weights.** Candidates not yet tested: funding rates,
   order-book imbalance, a regime filter that sits out chop. Note that cross-sectional ranking — the most
   promising structural idea — was tested here and failed.

## Reproducing

```bash
python -m app.backtest BTCUSDT --tf 4h --bars 3000                  # single market, calibration table
python -m app.backtest BTCUSDT --tf 4h --bars 3000 --exit-mode scaled   # compare against the old default
python -m app.backtest --journal data/signals.jsonl                 # forward-test real live signals
```

The sweep, AUC and cross-sectional harnesses were one-off scripts, not committed. The single most useful
ongoing measurement is the journal replay: `SignalJournal` records every live report, and
`--journal` scores them against candles that arrived afterwards. That accumulates genuine out-of-sample
evidence with no backtest assumptions at all.
