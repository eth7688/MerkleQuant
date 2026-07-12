# TradingView RJ Strategy Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a single-symbol TradingView RJ strategy mirror and a corrected AXIOM backtest that both use the demo engine's 30-minute ATR chandelier exit profile.

**Architecture:** Extend the shared Python exit contract with causal ATR chandelier inputs, then keep the portfolio replay on one-minute bars while deriving the chandelier ATR from closed 30-minute candles. Build a standalone Pine strategy that mirrors the same frozen entry and exit order. Validate both with deterministic state-machine tests and fixed event comparisons.

**Tech Stack:** Python 3.12, unittest, pandas, Pine Script v5, TradingView Strategy Tester.

## Global Constraints

- Entry decisions use closed candles only and never enable lookahead.
- RJ defaults remain KDJ 9/3/3, J=3K-2D, K-mode purple line x0.88.
- Primary triggers are J recovery from 0/100; crosses are fallback only.
- Volume, support/resistance, and divergence stay anchored to the signal key candle.
- Entry is the next bar open after the confirming 30-minute close.
- Initial stop is signal-key high/low outside by 0.5 ATR.
- Exit profile is 0.8R -> +0.25R, 1.2R -> +0.2R, 2.0R -> exit 50% and +1.0R.
- Remaining quantity uses a 30-minute ATR(14) x 3.5 chandelier that only tightens.
- No unignited-timeout exit is included.
- Pine remains a single-symbol cross-check; Python remains the portfolio authority.

---

### Task 1: Add ATR Chandelier to the Shared Exit Contract

**Files:**
- Modify: `strategy_core.py`
- Modify: `tests/test_strategy_core.py`

**Interfaces:**
- Consumes: `PositionState`, `ExitRules`, `advance_position(position, bar, rules)`.
- Produces: `ExitRules.atr_trail_period`, `ExitRules.atr_trail_mult`, and bar input `atr_30m`; `PositionState.highest_price` and `lowest_price` retain the favorable extreme.

- [ ] **Step 1: Write failing tests for long and short chandelier ratchets**

```python
def test_atr_chandelier_tightens_after_tier1_and_never_loosens(self):
    rules = ExitRules(early_protect_r=.8, early_lock_r=.25, tier1_r=1.2,
                      tier1_lock_r=.2, tier2_r=2.0, atr_trail_mult=3.5)
    pos = PositionState("BTCUSDT", "LONG", 100, 90, 1)
    pos, _ = advance_position(pos, {"ot": 1, "h": 113, "l": 101, "c": 112, "atr_30m": 2}, rules)
    self.assertEqual(pos.current_stop, 106)
    pos, _ = advance_position(pos, {"ot": 2, "h": 112, "l": 107, "c": 108, "atr_30m": 3}, rules)
    self.assertEqual(pos.current_stop, 106)
```

Add the symmetric short assertion: lowest seen 87 and ATR 2 produce a chandelier candidate of 94, and a later wider ATR cannot loosen it.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_strategy_core.StrategyCoreTest.test_atr_chandelier_tightens_after_tier1_and_never_loosens -v`

Expected: FAIL because `ExitRules` does not accept `atr_trail_mult`.

- [ ] **Step 3: Implement minimal state fields and ratchet order**

Add to `ExitRules`:

```python
atr_trail_period: int = 14
atr_trail_mult: float = 3.5
```

Add to `PositionState`:

```python
highest_price: float | None = None
lowest_price: float | None = None
```

Initialize extremes from entry, update them from bar high/low, preserve adverse-first stop evaluation, then apply early protection, tier 1, tier 2, and finally the chandelier. The chandelier is active after tier 1 or tier 2 and uses `bar["atr_30m"]`.

- [ ] **Step 4: Run focused and full tests**

Run: `python -m unittest tests.test_strategy_core -v`

Expected: all strategy-core tests PASS.

Run: `python -m unittest discover -s tests -v`

Expected: all repository tests PASS.

- [ ] **Step 5: Commit the exit contract**

```bash
git add strategy_core.py tests/test_strategy_core.py
git commit -m "fix: mirror ATR chandelier in backtest exits"
```

### Task 2: Feed Closed 30-Minute ATR into One-Minute Portfolio Replay

**Files:**
- Modify: `backtest/engine.py`
- Modify: `backtest/cli.py`
- Modify: `tests/test_backtest_system.py`
- Create: `backtest_data/rj_liquid192_50d_demo_exit.json`

**Interfaces:**
- Consumes: closed 30-minute frames and `advance_position(..., bar["atr_30m"], ...)`.
- Produces: each one-minute bar carries the latest ATR calculated only from 30-minute candles closed at or before that minute.

- [ ] **Step 1: Write a failing no-lookahead ATR test**

Create a 30-minute fixture where the next candle has an extreme range. Assert that one-minute bars before its close retain the prior ATR and only bars at/after its close receive the new value.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_backtest_system.BacktestSystemTest.test_portfolio_atr_uses_latest_closed_30m_candle -v`

Expected: FAIL because replay bars do not contain `atr_30m`.

- [ ] **Step 3: Implement causal ATR mapping**

Precompute true range and RMA(14) on each 30-minute frame. Map ATR to one-minute timestamps with `merge_asof` semantics on `30m.ot + 1_800_000`, direction backward. Do not recalculate ATR from one-minute bars.

Pass the demo exit values into `ExitRules`:

```python
early_protect_r=.8
early_lock_r=.25
tier1_r=1.2
tier1_lock_r=.2
tier2_r=2.0
tier2_fraction=.5
atr_trail_period=14
atr_trail_mult=3.5
```

- [ ] **Step 4: Freeze corrected experiment and rerun verification**

The new experiment copies entry rules from the frozen manifest, changes only the corrected demo exit profile, retains the 192-symbol 50-day data window, and keeps `minimum_core_trades=200`.

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 5: Run the corrected portfolio backtest**

```bash
python replay_engine.py portfolio --root backtest_data --symbols backtest_data/universe_liquid_192_50d_20260712.txt --experiment backtest_data/rj_liquid192_50d_demo_exit.json --output backtest_runs --workers 6
```

Expected: `metrics.json`, `events.jsonl`, and `reconciliation.json` are written; status is READY only when independent positions are at least 200 and reconciliation difference is below 1e-8.

- [ ] **Step 6: Commit replay changes**

```bash
git add backtest/engine.py backtest/cli.py tests/test_backtest_system.py backtest_data/rj_liquid192_50d_demo_exit.json
git commit -m "fix: replay demo ATR chandelier exits"
```

### Task 3: Implement the Standalone Pine Strategy Mirror

**Files:**
- Create: `tradingview_rj_strategy_mirror.pine`

**Interfaces:**
- Consumes: chart OHLCV plus closed BTC 1h/4h series from `request.security()`.
- Produces: TradingView entries, exits, chart evidence, and a compact metrics table.

- [ ] **Step 1: Add strategy declaration and frozen inputs**

Use Pine v5, `overlay=true`, `pyramiding=0`, commission 0.06%, and default next-bar-open order processing. Expose only date range, BTC symbol, commission, slippage, and visual toggles.

- [ ] **Step 2: Port causal RJ and key-candle setup logic**

Implement KDJ/J/purple calculations, primary level triggers, fallback crosses, six-bar confirmation, opposite invalidation, ATR confirmation buffer, chase limit, and key-candle stop.

- [ ] **Step 3: Port signal-key filters and historical gate**

Evaluate volume 20 x2.0, support/resistance, divergence, and rolling historical statistics from information available at the current closed bar. Do not use a pivot before its right-side bars are closed.

- [ ] **Step 4: Port BTC stage veto**

Use `request.security(..., lookahead=barmerge.lookahead_off)` for closed BTC 1h/4h evidence. Only explicit extreme early/mid trends veto the opposite direction.

- [ ] **Step 5: Implement demo exit profile**

Track initial risk, highest/lowest seen, active stop, partial state, and ATR(14) x3.5. Apply the same early/tier1/tier2/chandelier sequence and never loosen the stop.

- [ ] **Step 6: Add Chinese visual evidence and summary table**

Show primary/fallback key candle, confirmation, entry, initial/active stop, BTC stage, gate result, closed trades, win rate, net profit, profit factor, maximum drawdown, average R, and direction counts.

- [ ] **Step 7: Static safety scan and commit**

Check for `lookahead_on`, negative plot offsets, future-index references, and accidental `indicator()` declaration.

```bash
git add tradingview_rj_strategy_mirror.pine
git commit -m "feat: add TradingView RJ strategy mirror"
```

### Task 4: Cross-Validate and Report

**Files:**
- Create: `docs/backtests/2026-07-12-rj-portfolio-demo-exit.md`
- Create: `docs/tradingview/RJ_STRATEGY_MIRROR.md`
- Modify: `PROGRESS.md`

**Interfaces:**
- Consumes: corrected portfolio run artifacts and TradingView fixed-case observations.
- Produces: an auditable result report and usage guide.

- [ ] **Step 1: Reconcile the corrected portfolio ledger**

Report independent positions, fill events, partial exits, win rate, mean/median R, profit factor, maximum drawdown R, MFE/MAE, fees, capacity rejects, and reconciliation difference.

- [ ] **Step 2: Compare fixed chart cases**

For at least three symbols, compare direction, trigger source, key time, confirmation time, initial stop, and protection transitions. Record differences without adjusting parameters.

- [ ] **Step 3: Document TradingView use and limitations**

State 30-minute chart requirement, next-open execution, Bar Magnifier recommendation, exchange-feed differences, single-symbol scope, and Python portfolio authority.

- [ ] **Step 4: Run final verification**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 5: Update local progress and commit**

Record local implementation and verification in `PROGRESS.md`. No server deployment is required for the Pine or offline backtest artifacts.

```bash
git add docs/backtests/2026-07-12-rj-portfolio-demo-exit.md docs/tradingview/RJ_STRATEGY_MIRROR.md PROGRESS.md
git commit -m "docs: report RJ mirror validation"
```
