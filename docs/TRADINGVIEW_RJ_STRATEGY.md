# TradingView RJ Strategy

## File

`tradingview_rj_strategy_mirror.pine` is the standalone 30-minute strategy. The existing main-chart and panel indicators remain unchanged.

## What It Tests

- RJ KDJ 9/3/3, J=`3K-2D`, purple K-line mode scaled by 0.88.
- J recovery from 0/100 plus J/purple cross fallback.
- Signal-key volume, support/resistance, divergence, six-bar close confirmation, and key-candle ATR stop.
- Adaptive choppy classification on the immutable signal key candle.
- Causal recent RJ win rate by direction over the latest 1000 bars.
- 0.8R protection, 1.2R defense and ATR chandelier, 2R 50% reduction, then ATR(14) x 3.5 trailing exit.

## Recommended Comparison

Run the same symbol and date range three times:

1. Set both gates to `仅记录` to obtain the raw baseline.
2. Set only `震荡过滤模式` to `硬过滤` and compare trade count, win rate, profit factor, net profit, and drawdown.
3. Then set `近期胜率模式` to `硬过滤` and compare the incremental change.

Do not tune thresholds from one symbol and report the same period as validation. Choose parameters on an earlier segment, then verify them on a later untouched segment.

## Statistical Definition

The recent win rate is not TradingView's strategy win rate. It is a causal RJ signal-quality gate:

- Every risk-valid raw confirmed RJ setup is sampled, including signals rejected by volume, support/resistance, choppy, chase, date, and strategy-position filters.
- Evaluation begins on the next bar, never on the confirmation bar.
- A sample wins at +1R, loses at its key-candle stop, or closes at its 12-bar R value.
- Same-bar stop and target ambiguity is counted as a stop.
- Long and short samples are calculated independently.

## Boundaries

This script rejects charts other than 30 minutes. It does not reproduce portfolio slot competition, BTC stage filtering, Hermes, exchange fills, funding fees, or API execution. TradingView commission defaults to 0.06%; configure slippage and Bar Magnifier in Strategy Properties for the exchange and symbol being tested. Protection and partial-exit triggers are evaluated from 30-minute OHLC, so Bar Magnifier is recommended when the order of intrabar stop and target touches matters.
