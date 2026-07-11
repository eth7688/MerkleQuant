# RJ Backtest V1 Design

## Objective

Build a trustworthy event-driven backtest for the current RJ strategy before
expanding to additional execution timeframes. V1 validates whether the 30-minute
RJ strategy has positive expectancy after realistic execution costs and whether
each filter adds measurable value.

## V1 Scope

- Trading universe: USDT perpetual contracts available on the target exchange
  at each historical timestamp.
- Signal and position-management timeframe: 30m.
- Coin 1h context: recorded for analysis only; it does not block entries in V1.
- BTC context: 1h and 4h closed candles drive the shared BTC stage classifier.
- Intrabar execution: 1m candles determine stop, protection, partial take-profit,
  and trailing-stop ordering whenever data is available.
- Strategy source: RJ primary signals are reported separately from RJSETUP
  candidate-pool signals. They are never merged into one result group.
- Hermes: excluded from historical decisions in V1 because its output is not
  deterministic and may leak current information into historical decisions.

## Shared Strategy Core

The backtester, demo engine, and automatic trading engine call the same
deterministic strategy functions for:

- RJ indicator calculation and trigger classification;
- closed key-candle breakout confirmation;
- signal-key-candle ATR stop placement;
- coin location and reversal evidence;
- BTC stage classification and extreme-trend veto;
- position sizing;
- early protection, staged profit management, and trailing exits.

Exchange API calls, wall-clock time, file writes, and UI state do not belong in
the shared strategy core. The caller supplies market snapshots and receives a
decision plus machine-readable evidence.

## Data Requirements

For every contract and BTC, the historical store contains timestamped OHLCV
candles with exchange, symbol, timeframe, and data version. Contract metadata
contains listing time, delisting time, tick size, quantity step, minimum order,
contract multiplier, and applicable fee schedule.

The universe must be reconstructed as it existed at each timestamp. Using only
contracts that survive today is prohibited because it introduces survivorship
bias.

Higher-timeframe candles are either sourced directly or aggregated from lower
timeframes. In both cases, only candles closed before the simulated decision
time are visible.

## Event Sequence

For each 30-minute close:

1. Finalize all 1m and 30m candles up to the simulation timestamp.
2. Finalize eligible coin 1h and BTC 1h/4h candles.
3. Update indicators using closed data only.
4. Detect the RJ signal and identify its signal key candle.
5. Wait for closed-candle breakout confirmation.
6. Evaluate coin position evidence and BTC stage rules.
7. Create an entry intent with entry, stop, risk, quantity, and evidence.
8. Fill at the next tradable price with configured fees and slippage.
9. Replay each subsequent 1m candle through the shared exit state machine.
10. Close or partially reduce the position and write an immutable event ledger.

Signals that occur while the maximum-position limit is full are recorded as
capacity rejects and retained for counterfactual analysis.

## Execution Model

- Market entries fill at the next 1m open plus direction-aware slippage.
- Fees apply to every entry and exit fill, including partial exits.
- Funding applies at the exchange's historical funding timestamps when data is
  available; missing funding is explicitly marked rather than silently set to
  zero.
- Quantity and price follow historical contract precision and minimum limits.
- If only 30m data exists and both stop and target are touched in one candle,
  the conservative adverse-first assumption is used and the trade is marked
  `ambiguous_intrabar`.
- Exchange rejections and unavailable contracts are recorded, not converted
  into simulated fills.

## Experiment Matrix

Backtests run as fixed rule bundles:

- A: RJ primary signal only.
- B: A plus closed key-candle confirmation.
- C: B plus coin location and reversal evidence.
- D: C plus BTC stage filter.
- E: D plus staged profit management.
- F: D with alternative exit policies for exit-only comparison.

RJSETUP is evaluated as a separate experiment family using the same bundles.
Only one rule group changes between adjacent experiments.

## Metrics

Results are calculated per independent position, not per partial fill:

- trade count and exposure time;
- win rate;
- total, mean, and median R;
- average win, average loss, and payoff ratio;
- profit factor;
- maximum equity drawdown and drawdown duration;
- MFE and MAE distributions;
- 1R, 2R, and 3R reach rates;
- realized-R to MFE capture ratio;
- long and short results;
- RJ primary and RJSETUP results;
- BTC stage and alignment results;
- fees, funding, and slippage contribution.

Manual exits, data errors, and execution errors are reported separately from
strategy outcomes.

## Validation Protocol

- The backtester validates pre-declared causal rules; it does not predict future
  prices, generate labels from future returns, or discover rules by searching
  for the most profitable historical pattern.
- Every experiment declares its rule bundle, parameters, primary metric, sample
  threshold, and evaluation periods before the run starts.
- Future candles, future universe membership, future contract metadata, and
  full-period normalization statistics are unavailable to the strategy.
- Parameter grids, genetic search, Bayesian optimization, and repeated tuning
  against the untouched test period are prohibited in V1.
- A result is invalid if the rule was invented after inspecting the same test
  period on which it is reported. Such a rule must be evaluated on a new,
  untouched forward period.
- Use chronological train, validation, and untouched test periods.
- Require at least 200 independent trades for a core backtest conclusion and at
  least 50 observations before evaluating a specific filter bucket.
- Tune only on train data; select rules on validation data; report final results
  once on the untouched test period.
- Add walk-forward validation across bull, bear, and range regimes.
- Run trade-order Monte Carlo analysis to estimate drawdown sensitivity.
- Freeze a rule version and data version for every published result.

The engine reports historical evidence and uncertainty, not a forecast or a
guarantee of future returns. Passing a backtest only permits the rule to enter a
paper or shadow forward-test stage; it does not authorize immediate risk
scaling.

The commercial target is not win rate alone. A candidate must show positive
out-of-sample mean R, acceptable profit factor, stable regime behavior, and a
maximum drawdown target below 20%. The account-level 50% drawdown threshold is a
disaster halt, not a strategy acceptance threshold.

## Audit Outputs

Each run produces:

- immutable trade and event ledgers;
- rule and data version manifests;
- per-stage rejection counts;
- counterfactual ledgers for BTC-blocked and capacity-blocked signals;
- aggregate metrics by experiment and market regime;
- a reconciliation report comparing replay decisions with known demo/live
  decisions for identical candle fixtures.

## Verification

- Indicator parity tests compare backtest values with live RJ values on the same
  closed candles.
- Golden-fixture tests cover signal candle selection, key-candle confirmation,
  ATR stop placement, BTC stages, partial exits, protection, and trailing exits.
- Lookahead tests fail if a strategy reads a candle that had not closed.
- Intrabar tests verify adverse-first handling and 1m ordering.
- Accounting tests reconcile fills, fees, funding, realized PnL, equity, and
  maximum drawdown.
- Engine parity tests feed one fixture into demo, automatic, and backtest paths
  and require identical strategy decisions.

## Delivery Order

1. Extract and test the deterministic shared strategy core.
2. Implement and test the BTC stage filter against that core.
3. Build the historical data store and universe metadata.
4. Build the event-driven replay and accounting engine.
5. Reconcile the replay against known live/demo fixtures.
6. Run the experiment matrix and publish the untouched test results.

## Scope Exclusions

- V1 does not optimize 15m, 1h, or 4h execution signals.
- V1 does not use coin 1h context as an entry filter.
- V1 does not call Hermes during historical replay.
- V1 does not automatically search large parameter grids.
- V1 does not promote a rule based on fewer than the required observations.
