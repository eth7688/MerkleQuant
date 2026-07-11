# BTC Market Stage Filter Design

## Objective

Replace the current broad BTC direction veto with a stage-aware filter. BTC may
hard-block an opposite coin signal only during an extreme one-way move in its
early or middle stage. The demo engine and automatic trading engine must use the
same implementation and emit the same audit fields.

## Decision Order

1. The coin produces an RJ primary signal.
2. The coin key candle is confirmed by a closed candle.
3. The coin's position, reversal evidence, and available reward space are
   evaluated.
4. BTC market stage is evaluated.
5. Hermes performs its independent blind direction review.

BTC is contextual unless the extreme-trend veto defined below is active.

## BTC Stage Model

The classifier emits one of these states:

- `early_bull`
- `mid_bull`
- `late_bull`
- `bull_decay`
- `early_bear`
- `mid_bear`
- `late_bear`
- `bear_decay`
- `range`
- `unknown`

Stage evidence is computed from closed BTC candles only. It includes:

- 1h and 4h trend direction and agreement;
- EMA20 and EMA60 slope and spread change;
- ADX level and change;
- ATR-normalized distance from EMA20;
- recent breakout from a moving-average squeeze zone;
- momentum and volume expansion or contraction;
- price/momentum divergence and volume-price exhaustion;
- recent structural breakout or failure.

The classifier returns the stage, direction, strength evidence, exhaustion
evidence, and a machine-readable reason list. A scalar score may be retained for
diagnostics but must not be the sole hard-filter condition.

## Extreme Trend Veto

An extreme bullish veto is active only when all of the following are true:

- BTC 1h and 4h directions are bullish;
- the classified stage is `early_bull` or `mid_bull`;
- trend strength and momentum are expanding;
- no configured exhaustion or reversal condition is present;
- all required BTC data is complete and based on closed candles.

An extreme bearish veto is the symmetric condition for `early_bear` or
`mid_bear`.

When an extreme bullish veto is active, every coin SHORT is rejected. When an
extreme bearish veto is active, every coin LONG is rejected. There are no
coin-level exceptions, even if the coin has a complete reversal package.

## Non-Extreme BTC Conditions

BTC does not hard-block a direction when it is in an ordinary trend, late stage,
decay stage, range, or unknown state.

If a coin signal is opposite an ordinary BTC direction, it must pass the
coin-specific reversal package:

- RJ primary signal is present;
- key-candle breakout is confirmed by a closed candle;
- location evidence exists: ATR-normalized EMA20 extension or proximity to a
  historical squeeze support/resistance zone;
- at least one configured volume-price reversal condition is present;
- the immediate opposing zone does not invalidate the required reward space.

BTC `unknown` never activates an extreme veto. It records a data-quality event
and allows the decision to continue through the coin rules.

## Audit and Counterfactual Data

Every evaluated entry records:

- BTC stage and direction;
- extreme-veto state;
- 1h and 4h evidence snapshots;
- exhaustion evidence;
- final pass/block reason;
- whether the coin direction opposed BTC;
- whether the coin reversal package passed;
- code version and rule version.

Every BTC-blocked signal remains in a counterfactual ledger. Its 6, 12, and 24
closed-candle MFE and MAE are calculated using the original entry, key-candle
stop, and direction. Filter effectiveness is reviewed only after at least 50
independent blocked signals.

## Shared Engine Boundary

BTC classification and filtering live in a deterministic, side-effect-free
strategy module. Automatic trading, the demo engine, analysis scripts, and the
future backtester call the same functions. UI code and exchange clients do not
reimplement the rules.

## Failure Handling

- Missing or stale BTC candles produce `unknown`, never an extreme veto.
- Partially available timeframes produce `unknown` unless all extreme-veto
  requirements remain verifiably satisfied; the default is no veto.
- Classifier errors emit an audit event and continue through coin-level rules.
- A decision must include the candle close time used, preventing live-candle
  leakage.

## Verification

- Unit tests cover all stages, both extreme veto directions, late/decay pass,
  unknown-data pass, and ordinary opposite-direction reversal requirements.
- Regression tests prove the current `bull_bias + bull_bias` broad veto no
  longer blocks late or decaying trends.
- Demo and automatic trading receive identical outputs for the same fixture.
- Replay tests prove that only closed candles are used.
- Counterfactual records are deduplicated by signal key.

## Scope Exclusions

- This change does not tune RJ parameters.
- This change does not alter key-candle stop placement.
- This change does not use Hermes confidence as a filter.
- This change does not use historical squeeze targets as a hard entry filter.
- This change does not define a daily trade-count target.
