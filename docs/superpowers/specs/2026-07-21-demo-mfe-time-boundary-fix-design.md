# Demo MFE Time-Boundary Fix Design

## Problem

`bj_now()` stores Beijing wall-clock values while retaining a UTC timezone marker. The mark-price MFE migration converts `Position.entry_time` directly with `timestamp()`, placing the boundary eight hours in the future. When no candle passes that boundary, the current code falls back to all 100 fetched candles. Pre-entry highs/lows then inflate MFE/MAE, trigger the 0.8R protection immediately, move the stop to entry, and close the new position at estimated breakeven.

## Scope

- Keep `bj_now()` unchanged because it is used broadly for display and existing persisted records.
- Add one small conversion helper for persisted/display-style position timestamps used against exchange UTC millisecond timestamps.
- In the mark-price MFE migration, calculate excursions only from candles at or after the normalized entry boundary.
- If no candle exists after entry, do not migrate MFE/MAE, do not mark the source as migrated, and do not use pre-entry candles.
- Preserve normal behavior for true UTC timestamps and for legacy positions that have valid post-entry candles.
- Do not alter Predicta entries, risk sizing, early-protection thresholds, take-profit configuration, or dashboard formatting.

## Verification

- A regression test reproduces the eight-hour-future boundary and proves pre-entry candles cannot trigger protection.
- Existing mark-price migration tests continue to prove valid historical positions are rebased once.
- Run the focused exit/routing tests, full unittest suite, Python compilation, and `git diff --check`.
- Before deployment, back up server `trader.py`, `screener.py`, configuration, positions, trades, and logs.
- Deploy only the required code file(s), restart `macd-bot`, verify service health and source checksum, then inspect fresh logs for startup errors.

## Rollback

Restore the timestamped server backup of `trader.py`, compile it, and restart `macd-bot`. No database or configuration migration is involved.
