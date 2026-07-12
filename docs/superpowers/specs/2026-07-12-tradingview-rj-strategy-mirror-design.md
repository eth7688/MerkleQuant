# TradingView RJ Strategy Mirror Design

## Purpose

Build a single-symbol TradingView strategy that cross-checks AXIOM's production RJ signals and trade lifecycle. It is a diagnostic and research companion, not a replacement for the multi-symbol portfolio backtester or live engine.

## Scope

- Add one standalone Pine Script strategy: `tradingview_rj_strategy_mirror.pine`.
- Leave the existing main-chart and panel indicators unchanged.
- Target a 30-minute chart and enter on the next bar open after a confirmed key-candle break.
- Use closed candles only. No lookahead, future pivots in entry decisions, or result-dependent parameter selection.
- Display signal, risk, execution, and summary evidence directly on the chart.

## Frozen Production Defaults

- KDJ RSV length 9, K smoothing 3, D smoothing 3.
- J line is `3K - 2D`.
- Purple line uses K-line mode, scaled around 50 by 0.88 and clamped to 0-100.
- J recovery from 0/100 is the primary trigger; J/purple cross is fallback only.
- Confirmation window is 6 closed 30-minute bars.
- Confirmation buffer is 0.08 ATR; maximum chase is 2%.
- Stop is outside the signal key candle by 0.5 ATR, bounded to 0.3%-8% risk.
- Signal-key volume filter uses 20 bars and a 2.0 multiplier.
- Support/resistance and divergence filters are anchored to the signal key candle.
- Historical gate requires at least 8 samples, 55% win rate, and non-negative average R.
- Early protection at 0.8R locks 0.25R.
- Tier 1 at 1.2R upgrades the defensive stop to +0.2R and enables the 30-minute ATR chandelier.
- Tier 2 at 2.0R exits 50% of the original position and upgrades the stop to +1.0R.
- The remaining position uses a 30-minute ATR(14) x 3.5 chandelier stop.
- Commission defaults to 0.06% per fill; slippage is exposed as a TradingView strategy setting.

## Entry Pipeline

1. Compute causal RJ lines on the chart symbol.
2. Detect primary level recovery triggers and fallback crosses.
3. Record the trigger bar as the immutable signal key candle.
4. Evaluate volume, support/resistance, and divergence on that key candle.
5. Track the setup for at most 6 subsequent closed bars.
6. Invalidate it if price closes through the opposite side or if a prior bar already confirmed it.
7. Require the current close to break the key level plus/minus 0.08 ATR without exceeding 2% chase.
8. Apply the rolling historical sample gate.
9. Apply the BTC stage gate using closed BTC 1h and 4h data from `request.security()`.
10. Submit the strategy order after the confirming close; TradingView fills it at the next bar open under default processing.

## BTC Stage Gate

The Pine implementation mirrors the production stage categories using closed 1h/4h EMA alignment, ADX, ATR distance, volume, momentum, and exhaustion evidence. Only explicit extreme early/mid trends hard-veto opposite signals. Late, decay, range, mixed, and unknown states do not hard-block.

## Exit Pipeline

- Initial stop is derived only from the signal key candle and ATR.
- Existing stop is evaluated before new protection levels within the available bar model.
- At 0.8R, move stop to +0.25R.
- At 1.2R, move stop to +0.2R and begin the 30-minute ATR chandelier ratchet.
- At 2.0R, exit 50%, move stop to +1.0R, and retain the remainder.
- For a long, chandelier stop is highest price seen minus ATR(14) x 3.5. For a short, it is lowest price seen plus ATR(14) x 3.5.
- The chandelier can only tighten the active stop and never loosen it.
- No legacy unignited-timeout exit is included.

## Visual Evidence

- Mark primary versus fallback key candles distinctly.
- Mark confirmation and next-bar entry.
- Plot initial and active stops without changing chart scale unexpectedly.
- Show BTC stage and gate result.
- Show a compact table with closed trades, win rate, net profit, profit factor, maximum drawdown, average R, and long/short counts.
- Keep all labels in clear Chinese and use restrained AXIOM colors.

## Known Fidelity Boundaries

- TradingView cannot model 192 symbols competing for six shared slots.
- Hermes and exchange API decisions are outside Pine.
- Bar Magnifier improves intrabar fills but cannot guarantee exact equivalence to Python's one-minute adverse-first replay on every ambiguous bar.
- Exchange feed differences can shift levels and fills.
- The Pine historical gate will be validated against fixed chart cases; any irreducible platform difference must be shown in the table rather than hidden.

## Verification

- Pine compiles without warnings that affect behavior.
- No repaint: all entry decisions use confirmed data and `request.security(..., lookahead_off)`.
- Default parameter values match the frozen production experiment manifest.
- At least three fixed symbol/time cases are compared against AXIOM events for trigger source, key time, confirmation time, direction, and stop.
- Strategy Tester reports are treated as single-symbol evidence only.
- Existing Python test suite remains green because current indicators and engine files are untouched.

## Non-Goals

- Portfolio allocation and shared position limits.
- Live order execution, alerts to Hermes, or exchange account synchronization.
- Parameter optimizer, presets, or broad research controls.
- Replacing the AXIOM portfolio backtest result.
