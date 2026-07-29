# Demo Entry Order Compatibility Design

## Goal

Prevent valid Predicta/EWO demo signals from being lost when Binance Futures applies market-order quantity limits or rejects the configured leverage for a symbol.

## Confirmed causes

- `_floor_qty` returns on the first matching `LOT_SIZE` or `MARKET_LOT_SIZE` filter. Binance lists `LOT_SIZE` first, so market orders can exceed the lower `MARKET_LOT_SIZE.maxQty`.
- Binance leverage is fixed at 25x. Some contracts reject 25x, but the entry path ignores that failure and submits the original order anyway.

## Design

1. Market orders must use `MARKET_LOT_SIZE` when that filter exists, falling back to `LOT_SIZE` only when it does not.
2. Binance Futures leverage setup must try the configured leverage first, then lower safe candidates without duplicates: 20x, 10x, 5x, 3x, 2x, and 1x, never trying a value above the configured leverage.
3. If no leverage succeeds, reject the entry before `entry_precheck_pass` with reason `leverage_unavailable`; do not submit a market order.
4. If Binance returns `maxNotionalValue`, cap quantity to 98% of that value, floor it using the market-order filter, and recompute notional and actual risk.
5. Record requested/effective leverage and any notional cap in entry events. Do not change Predicta signal generation, EWO rules, choppy filtering, stop calculation, exit logic, configuration, or existing positions.

## Verification

- Regression test with `LOT_SIZE.maxQty=1,000,000` and `MARKET_LOT_SIZE.maxQty=500` must floor a 3,089-unit market quantity to 500.
- Regression test must prove a rejected 25x request retries 20x and returns the successful exchange response.
- Entry-preparation tests must prove max-notional clamping recomputes quantity/notional/risk and that total leverage failure blocks ordering.
- Run the focused tests, all unit tests, and Python compilation before deployment.
- Back up server code/config/state, upload only `trader.py`, restart `macd-bot`, verify service health, hashes, scan continuity, and non-ordering probes.

## Non-goals

- No speculative timestamp retry change: current server and Binance clocks are synchronized, so the intermittent `-1021` cause is not yet proven.
- No rate-limit architecture change.
- No exit-profile deployment.
