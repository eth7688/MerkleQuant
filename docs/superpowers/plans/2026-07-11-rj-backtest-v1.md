# RJ Backtest V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a non-predictive, non-fitted, event-driven 30m RJ backtest with 1m execution replay and parity with the live/demo strategy core.

**Architecture:** Extract deterministic RJ decisions into focused pure modules, then run those modules through an event engine backed by timestamped Bitget USDT perpetual data. The old `replay_engine.py` becomes a CLI compatibility wrapper; it does not remain the source of strategy logic or accounting.

**Tech Stack:** Python 3.12, pandas, numpy, requests, unittest, JSONL manifests and ledgers; no new dependency is required for V1.

## Global Constraints

- Signal and management timeframe is 30m; coin 1h is record-only.
- BTC stage uses closed 1h and 4h candles.
- 1m candles determine intrabar execution order when available.
- No future candles, future universe membership, future metadata, or full-period normalization.
- No parameter search, genetic optimization, Bayesian optimization, or reuse of the untouched test period.
- Hermes is excluded from historical decisions.
- RJ primary and RJSETUP results remain separate.
- Every published run freezes rule version, data version, and period boundaries.

---

### Task 1: Golden Fixtures and Strategy Decision Contract

**Files:**
- Create: `backtest/__init__.py`
- Create: `strategy_core.py`
- Create: `tests/fixtures/rj_30m_fixture.json`
- Create: `tests/test_strategy_core_contract.py`

**Interfaces:**
- Produces: `StrategySnapshot`, `EntryDecision`, and `ExitDecision` dataclasses.
- Produces: `evaluate_entry(snapshot: StrategySnapshot, rules: dict) -> EntryDecision`.
- Produces: `evaluate_exit(position: dict, market: dict, rules: dict) -> ExitDecision`.

- [ ] **Step 1: Export a fixed closed-candle fixture from known RJ data**

Store OHLCV candles, expected RJ trigger source, signal key candle, confirmation close, ATR stop, and current rule version. Exclude API keys, account values, and future candles.

- [ ] **Step 2: Write contract tests before extraction**

Assert decisions are JSON-safe, include `rule_version`, expose every evidence field used by live audit logs, and reject a snapshot whose decision timestamp precedes a supplied candle close.

- [ ] **Step 3: Run and verify failure**

Run: `python -m unittest tests.test_strategy_core_contract -v`

Expected: FAIL because `strategy_core` does not exist.

- [ ] **Step 4: Implement dataclasses and validation only**

Do not move strategy formulas yet. Implement immutable inputs/outputs and closed-candle timestamp validation.

- [ ] **Step 5: Run contract tests**

Expected: all contract tests PASS.

### Task 2: Extract RJ Entry Decisions Without Changing Behavior

**Files:**
- Modify: `strategy_core.py`
- Modify: `rj_indicator.py`
- Modify: `trader.py` RJ signal and setup entry paths.
- Create: `tests/test_strategy_core_entry_parity.py`

**Interfaces:**
- Consumes: `compute_rj_bbkd(df, params)` from `rj_indicator.py`.
- Produces: `evaluate_rj_entry(candles_30m, rules, btc_stage, coin_1h=None) -> EntryDecision`.

- [ ] **Step 1: Write parity tests against current closed-candle fixtures**

Cover J0/J100 primary signals, cross fallback, key-candle close confirmation, volume anchored to the signal candle, key-candle ATR stop, and RJSETUP classification. Assert coin 1h fields are recorded but cannot change `allowed` in V1.

- [ ] **Step 2: Verify parity tests fail before extraction**

Run: `python -m unittest tests.test_strategy_core_entry_parity -v`

Expected: FAIL because `evaluate_rj_entry` is missing.

- [ ] **Step 3: Move deterministic entry calculations into `strategy_core.py`**

Pass all parameters explicitly. Do not read files, clocks, clients, bot state, or mutable global configuration. Keep trader wrappers thin and preserve existing event fields.

- [ ] **Step 4: Run RJ, volume, BTC, and target-zone regression tests**

Run: `python -m unittest tests.test_strategy_core_entry_parity tests.test_rj_volume_anchor tests.test_target_zone_selection tests.test_btc_stage_gate -v`

Expected: all tests PASS.

### Task 3: Extract Position Management State Machine

**Files:**
- Modify: `strategy_core.py`
- Modify: `trader.py` position-management loop.
- Create: `tests/test_strategy_core_exit_parity.py`

**Interfaces:**
- Produces: `advance_position(position_state, bar_1m, rules) -> tuple[position_state, list[ExitDecision]]`.
- `ExitDecision.action` is one of `hold`, `move_stop`, `partial_exit`, `full_exit`.

- [ ] **Step 1: Write state-transition tests**

Cover signal-key ATR initial stop, 0.8R early protection, first defense, second partial exit, break-even/locked stop, trailing exit, and exchange precision. Explicitly assert fixed time-stop exits are absent from the shared V1 policy.

- [ ] **Step 2: Verify tests fail**

Run: `python -m unittest tests.test_strategy_core_exit_parity -v`

Expected: FAIL because the pure state machine does not exist.

- [ ] **Step 3: Extract transitions without exchange calls**

Return intents only. The live adapter remains responsible for placing/canceling orders; the backtester applies simulated fills.

- [ ] **Step 4: Run all position-management tests**

Run: `python -m unittest tests.test_strategy_core_exit_parity tests.test_exchange_entry_price_sync -v`

Expected: all tests PASS.

### Task 4: Historical Data Store and Point-in-Time Universe

**Files:**
- Create: `backtest/data_store.py`
- Create: `backtest/bitget_history.py`
- Create: `tests/test_backtest_data_store.py`

**Interfaces:**
- Produces: `BitgetHistorySource.fetch_candles(symbol, interval, start_ms, end_ms) -> pd.DataFrame`.
- Produces: `HistoricalStore.read_candles(...)`, `write_candles(...)`, `universe_at(timestamp_ms)`.
- Storage schema: `exchange,symbol,interval,ot,ct,o,h,l,c,v,data_version` plus contract metadata effective timestamps.

- [ ] **Step 1: Write paginated-data and point-in-time universe tests**

Use mocked public API responses. Assert deduplication by `(exchange,symbol,interval,ot)`, strict chronological ordering, gap detection, and exclusion before listing/after delisting.

- [ ] **Step 2: Verify tests fail**

Run: `python -m unittest tests.test_backtest_data_store -v`

Expected: FAIL because the data modules do not exist.

- [ ] **Step 3: Implement JSONL manifest plus per-symbol CSV storage**

Avoid adding a database dependency in V1. Write atomically through a temporary file and rename. Never silently fill missing OHLCV bars; expose gaps in the manifest.

- [ ] **Step 4: Implement rate-limited Bitget public-data pagination**

Reuse the existing HTTP conventions but keep this adapter credential-free. Persist raw response hashes and retrieval timestamps in the manifest.

- [ ] **Step 5: Run data-store tests**

Expected: all tests PASS.

### Task 5: Event Replay and Accounting

**Files:**
- Create: `backtest/events.py`
- Create: `backtest/execution.py`
- Create: `backtest/engine.py`
- Create: `tests/test_backtest_execution.py`
- Create: `tests/test_backtest_accounting.py`

**Interfaces:**
- Produces: `ReplayEngine.run(period, universe, rules) -> BacktestResult`.
- Produces append-only events: `signal`, `reject`, `entry_fill`, `stop_move`, `partial_fill`, `exit_fill`, `funding`, `equity`.

- [ ] **Step 1: Write ordering and accounting tests**

Assert next-1m-open entries, direction-aware slippage, fees on every fill, funding at supplied timestamps, partial quantity conservation, and exact equity reconciliation. Assert adverse-first resolution when only a 30m bar touches stop and target.

- [ ] **Step 2: Verify tests fail**

Run: `python -m unittest tests.test_backtest_execution tests.test_backtest_accounting -v`

Expected: FAIL because replay components are missing.

- [ ] **Step 3: Implement immutable event objects and simulated broker**

Use integer timestamps and Decimal-compatible exchange rounding at fill boundaries. Keep strategy R calculations based on the original per-unit risk.

- [ ] **Step 4: Implement chronological replay**

At each timestamp, expose only closed data, evaluate exits before new entries according to the declared event order, enforce maximum positions, and write capacity rejects for counterfactual analysis.

- [ ] **Step 5: Run execution and accounting tests**

Expected: all tests PASS with zero reconciliation difference at configured precision.

### Task 6: Metrics, Anti-Fit Manifest, and Experiment Matrix

**Files:**
- Create: `backtest/metrics.py`
- Create: `backtest/experiment.py`
- Create: `tests/test_backtest_metrics.py`

**Interfaces:**
- Produces: `summarize_positions(events) -> dict`.
- Produces: `ExperimentSpec` containing frozen rule bundle, parameters, data version, train/validation/test boundaries, primary metric, and creation timestamp.

- [ ] **Step 1: Write independent-position metric tests**

Assert partial exits count as one trade; verify mean/median R, PF, payoff, drawdown, drawdown duration, MFE/MAE, 1R/2R/3R reach, capture ratio, fees, funding, and slippage.

- [ ] **Step 2: Write anti-fit validation tests**

Reject missing period declarations, overlapping train/test periods, changed rules after test execution, fewer than 200 core trades for a conclusion, and fewer than 50 observations for a filter claim.

- [ ] **Step 3: Implement metrics and immutable experiment manifests**

Support bundles A-F from the design. Keep RJ and RJSETUP in distinct result groups and never auto-select the best parameter set.

- [ ] **Step 4: Run metrics tests**

Run: `python -m unittest tests.test_backtest_metrics -v`

Expected: all tests PASS.

### Task 7: CLI Migration and Three-Engine Parity

**Files:**
- Replace implementation: `replay_engine.py`
- Create: `tests/test_replay_cli.py`
- Create: `tests/test_three_engine_parity.py`

**Interfaces:**
- CLI inputs: experiment manifest path and historical-store path.
- CLI outputs: immutable event ledger, position ledger, metrics JSON, and run manifest.

- [ ] **Step 1: Write CLI and parity tests**

Assert the old squeeze-scanner monkeypatch path is not called. Feed identical fixtures to strategy-core adapters for replay, demo, and automatic modes and require identical entry direction, key candle, stop, BTC decision, and exit intents.

- [ ] **Step 2: Verify tests fail against the old replay engine**

Run: `python -m unittest tests.test_replay_cli tests.test_three_engine_parity -v`

Expected: FAIL because the current replay engine calls `screener.scan_squeeze_breakout` and uses close-only exits.

- [ ] **Step 3: Replace replay CLI internals**

Keep a clear error for the old `--csv` interface directing callers to create an experiment manifest; do not silently run the old strategy.

- [ ] **Step 4: Run full local verification**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Run: `python -m py_compile strategy_core.py btc_stage.py replay_engine.py backtest\*.py`

Expected: all tests PASS and compile exits 0.

### Task 8: Baseline Run and Reconciliation Report

**Files:**
- Create at runtime: `backtest_runs/<run_id>/manifest.json`
- Create at runtime: `backtest_runs/<run_id>/events.jsonl`
- Create at runtime: `backtest_runs/<run_id>/positions.jsonl`
- Create at runtime: `backtest_runs/<run_id>/metrics.json`
- Create at runtime: `backtest_runs/<run_id>/reconciliation.json`
- Modify: `.gitignore`

**Interfaces:**
- Produces one reproducible run directory identified by a content-derived run id.

- [ ] **Step 1: Add generated datasets and run outputs to `.gitignore`**

Ignore `backtest_data/` and `backtest_runs/`; keep fixture files tracked when version control exists.

- [ ] **Step 2: Download a bounded pilot period and validate gaps**

Use BTC plus a small declared contract subset to verify data and engine correctness before downloading the full universe. Abort the run if required 30m/1m/BTC context gaps exceed the manifest policy.

- [ ] **Step 3: Reconcile known live/demo fixtures**

Require exact signal direction, key candle, stop source, BTC decision, and position sizing inputs. Report price differences caused only by declared slippage/fill assumptions.

- [ ] **Step 4: Run the frozen A-F experiment matrix**

Do not alter rules after viewing untouched-test results. Mark conclusions `SAMPLE_NOT_READY` when thresholds are unmet.

- [ ] **Step 5: Publish the factual report**

Report raw counts, uncertainty, data gaps, and invalid runs. Do not annualize short samples or describe historical results as forecasts.

## Version-Control Note

The workspace currently has no `.git` directory. Do not initialize or create a
repository implicitly. Use test and manifest checkpoints until version control
is explicitly available.
