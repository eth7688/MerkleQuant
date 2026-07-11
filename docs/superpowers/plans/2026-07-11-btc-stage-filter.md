# BTC Stage Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broad BTC direction veto with one deterministic stage-aware gate shared by automatic trading, the demo engine, and future replay tools.

**Architecture:** Add a pure `btc_stage.py` module that accepts closed 1h/4h OHLCV frames and returns a JSON-safe stage snapshot. `trader.py` becomes an adapter: it fetches candles, calls the module, applies the gate, and writes audit events. No UI or exchange client reimplements stage rules.

**Tech Stack:** Python 3.12, pandas, numpy, unittest, existing Flask/trader runtime.

## Global Constraints

- Use closed candles only and include their close timestamps in every decision.
- Hard veto only opposite trades during extreme early/middle BTC trends.
- Late, decay, range, and unknown BTC states never hard-veto a direction.
- Extreme veto has no coin-level exception.
- Demo and automatic trading must call the same functions.
- Keep all current exchange credentials and production data untouched.
- Update local files before deployment; update local `PROGRESS.md` after verified deployment.

---

### Task 1: Pure BTC Indicator Snapshot

**Files:**
- Create: `btc_stage.py`
- Create: `tests/test_btc_stage_indicators.py`

**Interfaces:**
- Produces: `build_interval_snapshot(df: pd.DataFrame) -> dict`
- Output keys: `direction`, `ema20`, `ema60`, `ema20_slope_atr`, `ema60_slope_atr`, `ema_spread_atr`, `ema_spread_change`, `adx`, `adx_change`, `distance_ema20_atr`, `atr`, `volume_ratio`, `breakout_age`, `momentum_change`, `exhaustion_flags`, `closed_at`.

- [ ] **Step 1: Write indicator tests with fixed OHLCV fixtures**

Create deterministic rising, falling, flat, and exhausted frames. Assert finite ATR/ADX values, direction symmetry, and `closed_at == df.iloc[-1].ot`.

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `python -m unittest tests.test_btc_stage_indicators -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'btc_stage'`.

- [ ] **Step 3: Implement the minimum pure calculations**

Use pandas EWM for EMA20/EMA60, Wilder RMA for ATR14 and ADX14, ATR-normalized five-bar slopes, 20-bar volume ratio, and closed-bar-only breakout/exhaustion checks. Reject frames shorter than 80 bars with `ValueError("btc_stage_insufficient_bars")`.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_btc_stage_indicators -v`

Expected: all tests PASS.

### Task 2: Stage Classification and Extreme Gate

**Files:**
- Modify: `btc_stage.py`
- Create: `tests/test_btc_stage_gate.py`

**Interfaces:**
- Consumes: `build_interval_snapshot(df)` from Task 1.
- Produces: `classify_btc_stage(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> dict`.
- Produces: `evaluate_btc_gate(direction: str, stage: dict, coin_reversal_pass: bool) -> tuple[bool, str]`.

- [ ] **Step 1: Write table-driven stage and gate tests**

Cover `early_bull`, `mid_bull`, `late_bull`, `bull_decay`, bearish symmetry, `range`, and `unknown`. Assert only extreme early/middle states block the opposite direction; assert `coin_reversal_pass=True` cannot override an extreme veto.

- [ ] **Step 2: Verify tests fail before implementation**

Run: `python -m unittest tests.test_btc_stage_gate -v`

Expected: FAIL because the classifier and gate do not exist.

- [ ] **Step 3: Implement explicit stage predicates**

Classify direction from 1h/4h agreement. Define extreme early/middle from expanding EMA spread, ADX strength and increase, aligned momentum/volume, acceptable EMA20 distance, and no exhaustion flags. Define late from excessive ATR distance or divergence/exhaustion; define decay from weakening ADX/spread/momentum; otherwise return range/unknown. Return `stage`, `direction`, `extreme_veto`, `evidence`, `exhaustion_flags`, `closed_1h_at`, `closed_4h_at`, and `rule_version="btc_stage_v1"`.

- [ ] **Step 4: Run stage tests**

Run: `python -m unittest tests.test_btc_stage_gate -v`

Expected: all tests PASS.

### Task 3: Trader Integration and Engine Parity

**Files:**
- Modify: `trader.py:4100-4254`
- Modify: `tests/test_btc_direction_filter.py`
- Create: `tests/test_btc_stage_trader_parity.py`

**Interfaces:**
- Consumes: `classify_btc_stage` and `evaluate_btc_gate`.
- Preserves: `_btc_regime_fields()` JSON fields used by the UI and ledgers.
- Changes: `_btc_direction_filter(direction, btc_fields, coin_reversal_pass=False)` delegates to the pure gate.

- [ ] **Step 1: Replace old broad-veto expectations with regression tests**

Assert `bull_bias + bull_bias` no longer blocks by itself. Assert `late_bull`, `bull_decay`, and `unknown` pass. Assert `early_bull` with `extreme_veto=True` blocks SHORT.

- [ ] **Step 2: Verify regression tests fail against current code**

Run: `python -m unittest tests.test_btc_direction_filter tests.test_btc_stage_trader_parity -v`

Expected: FAIL because current code blocks any aligned `bull_bias` pair.

- [ ] **Step 3: Replace `_btc_interval_regime` aggregation with the shared classifier adapter**

Fetch at least 200 closed 1h and 4h BTC candles, remove the currently open candle, call `classify_btc_stage`, flatten the result without discarding existing compatibility fields, and cache for 300 seconds keyed by candle close time.

- [ ] **Step 4: Route both entry paths through one gate**

Ensure direct RJ and RJSETUP candidate entries call the same `_btc_direction_filter`. Do not add a second gate in `web_ui.py` or `admin_server.py`.

- [ ] **Step 5: Run parity and full tests**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Expected: all tests PASS.

### Task 4: Audit and Counterfactual Ledger

**Files:**
- Modify: `trader.py` at BTC rejection event construction near the RJ entry path.
- Create: `btc_filter_events_0.jsonl` at runtime only; do not commit generated data.
- Modify: `analyze_btc_filter.py`
- Modify: `tests/test_analyze_btc_filter.py`

**Interfaces:**
- Produces event fields: `signal_key`, `symbol`, `direction`, `decision_time`, `entry_reference`, `signal_stop`, `btc_stage`, `btc_extreme_veto`, `btc_evidence`, `btc_exhaustion_flags`, `btc_rule_version`, `coin_reversal_pass`, `reason`.
- Produces analysis horizons: 6, 12, and 24 closed 30m bars.

- [ ] **Step 1: Write deduplication and horizon tests**

Assert one signal key creates one counterfactual record and that MFE/MAE use the original direction, entry reference, and signal-key stop.

- [ ] **Step 2: Verify analysis tests fail**

Run: `python -m unittest tests.test_analyze_btc_filter -v`

Expected: FAIL on missing stage/audit fields or 24-bar horizon.

- [ ] **Step 3: Persist complete rejection snapshots and extend analyzer**

Write append-only events. The analyzer must report block count, 1R/2R reach rate, stop-first rate, mean MFE/MAE, and results by BTC stage. It must print `SAMPLE_NOT_READY` before 50 independent blocked signals.

- [ ] **Step 4: Run analyzer tests and compile checks**

Run: `python -m unittest tests.test_analyze_btc_filter -v`

Run: `python -m py_compile btc_stage.py trader.py analyze_btc_filter.py`

Expected: PASS with no output from `py_compile`.

### Task 5: Local Verification and Production Deployment

**Files:**
- Modify after deployment: `PROGRESS.md`

**Interfaces:**
- Deployment target: `<deploy-dir>` on `<production-host>`.

- [ ] **Step 1: Run the complete local verification suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Run: `python -m py_compile btc_stage.py trader.py web_ui.py admin_server.py analyze_btc_filter.py`

Expected: all tests PASS and compile exits 0.

- [ ] **Step 2: Back up server files before upload**

Back up `trader.py`, `analyze_btc_filter.py`, and any existing `btc_stage.py` with a timestamp under `/root/`. Do not upload `memory/`, tests, generated ledgers, databases, or local credentials.

- [ ] **Step 3: Upload local files and verify server compilation**

Upload only `btc_stage.py`, `trader.py`, and `analyze_btc_filter.py`. Run the server venv `py_compile` before restart.

- [ ] **Step 4: Restart and verify runtime health**

Restart `macd-bot`; verify `systemctl is-active macd-bot`, `/trader/status?fast=1`, `/demo/status?fast=1`, and a read-only BTC snapshot. Confirm demo and uid=2 report the same stage for the same cache timestamp.

- [ ] **Step 5: Update local progress history**

Append the exact change, uploaded files, backup paths, restart action, test results, endpoint checks, and any unverified market-state branch to local `PROGRESS.md`.

## Version-Control Note

The workspace currently has no `.git` directory. Do not initialize or create a
repository implicitly. Use task-level verification checkpoints in place of
commits until the user provides or initializes version control.
