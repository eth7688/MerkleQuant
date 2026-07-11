# AVAXUSDT 30-Day RJ Backtest

## Run Identity

- Run id: `635101864bd3d349`
- Exchange data: Bitget USDT perpetual public candles
- Data version: `bitget-30d-20260711`
- Signal timeframe: 30m
- Execution timeframe: 1m
- Data range: 2026-06-11 12:00 UTC to 2026-07-11 11:30 UTC
- Train context: 2026-06-11 to 2026-06-16
- Validation context: 2026-06-16 to 2026-06-21
- One-time test: 2026-06-21 to 2026-07-11
- Rules: uid=2 non-secret live strategy values captured before the run

All four input series had zero detected gaps: AVAX 1m 43,199 rows, AVAX 30m
1,439 rows, BTC 1h 719 rows, and BTC 4h 179 rows.

## Aggregate Result

| Metric | Value |
|---|---:|
| Independent positions | 4 |
| Wins | 3 |
| Win rate | 75.0% |
| Total R | -0.387134R |
| Mean R | -0.096784R |
| Median R | +0.175384R |
| Profit factor | 0.672552 |
| Maximum drawdown | 1.182276R |
| Reached 1R | 2 |
| Reached 2R | 0 |
| Mean MFE | 0.877285R |
| Mean MAE | 0.244034R |
| Fees | 1.30017873 USDT |
| Initial equity | 5,000.00 USDT |
| Final equity | 4,996.128659885429 USDT |
| Status | SAMPLE_NOT_READY |

The immutable ledger reconciled exactly to simulated account equity with a
reported difference of `0.0` USDT. Historical funding remains explicitly
`not_modeled`.

## Positions

| Entry UTC | Direction | Trigger | Realized R | MFE | MAE | Exit |
|---|---|---|---:|---:|---:|---|
| 2026-06-29 15:30 | LONG | jr_cross_fallback | +0.444375R | 1.396064R | 0.008423R | protected stop |
| 2026-07-01 14:30 | LONG | jr_cross_fallback | +0.187522R | 0.894664R | 0.344644R | protected stop |
| 2026-07-01 23:00 | SHORT | j100_recover | +0.163246R | 1.093790R | 0.012720R | protected stop |
| 2026-07-06 11:00 | LONG | j0_recover | -1.182276R | 0.124622R | 0.610349R | initial stop |

## Interpretation Boundary

This run validates the end-to-end single-symbol replay and accounting path. It
does not establish expectancy: four positions are far below the predeclared
minimum of 200. The observed 75% win rate still produced negative expectancy
because the average captured winner was small relative to the full losing
position. No rule or parameter is changed from this result.
