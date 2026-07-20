# Predicta 3R Exhaustion Exit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a demo-only Predicta V2 exit profile that protects at 0.8R/1.2R, partially realizes a three-factor hard exhaustion before 3R, takes 80% of the current position at 3R, and trails the remaining runner only after 3R.

**Architecture:** Put all closed-candle exhaustion evidence and percentage math in a new side-effect-free `predicta_exit.py` module. Keep exchange execution, persistence, stop-order replacement, and legacy V1 behavior in `trader.py`; make the replay engine call the same pure evaluator at each closed 30-minute boundary so live and historical rules cannot drift.

**Tech Stack:** Python 3.12, dataclasses, pandas, NumPy, Flask trading engine, Binance/Bitget exchange adapters, JSON persistence, `unittest`, existing portfolio replay engine.

## Global Constraints

- Only Predicta/EWO positions may use V2; RJ and structure positions remain V1.
- `TradeConfig.predicta_exit_profile_version` defaults to `1`; only the demo configuration is changed to `2`.
- Existing positions missing an explicit version load as V1 and never migrate in place.
- R is permanently anchored to the real fill entry and original stop: `initial_risk_per_unit = abs(fill_entry_price - initial_sl)`.
- V2 thresholds are exactly: 0.8R breakeven, 1.2R locks 0.2R, 3 closed bars, less than 0.1R MFE improvement, at least 0.5R MFE drawdown, 50% original hard-exhaustion close, 1R post-exhaustion lock, 3R target, 80% current-position target close, 2R runner floor, ATR(14) × 3.5 runner.
- Hard exhaustion requires all three closed-candle facts: opposite EWO color, stalled progress with drawdown, and a close through the latest post-entry confirmed structure fractal.
- EWO color follows the supplied Pine definition: red when `EWO <= 0`, green when `EWO > 0`.
- A live 3R mark-price trigger has priority over exhaustion observed on the same engine cycle.
- ATR trailing is disabled for V2 before the 3R partial succeeds; no fixed 5R/8R target and no Predicta time stop are added.
- A stage flag is written only after an accepted partial order is reconciled to the exchange position; accepted but unreconciled orders are persisted as pending and are never resent blindly.
- Existing exchange quantity formatting, stop-order ID persistence, and reduce-only semantics remain intact.
- No admin UI controls are added in this phase.
- Only local runtime source is updated before deployment; tests, docs, plans, `PROGRESS.md`, and memory files are not uploaded.
- After deployment, update local `PROGRESS.md` with changed files, backup path, deployment actions, hashes, position continuity, and verification results.

---

### Task 1: Build the Pure Hard-Exhaustion Evaluator

**Files:**
- Create: `predicta_exit.py`
- Create: `tests/test_predicta_exit.py`

**Interfaces:**
- Consumes: closed OHLC candles, direction, entry timestamp/price, fixed unit risk, lifecycle MFE, and `PredictaExitRules`.
- Produces: `evaluate_hard_exhaustion(candles, direction, entry_time_ms, entry_price, initial_risk_per_unit, lifecycle_mfe_r, rules) -> ExhaustionEvidence` and `runner_stop_price(direction, entry, risk, current_stop, extreme, atr, rules) -> float` without file, network, logger, or exchange side effects.

- [ ] **Step 1: Write failing tests for color, stagnation, structure, and 3-of-3 semantics**

Create tests with these imports and helpers:

```python
import unittest
from unittest.mock import patch

import pandas as pd

from predicta_exit import (
    PredictaExitRules,
    evaluate_hard_exhaustion,
    runner_stop_price,
)


def frame(closes):
    closes = [float(value) for value in closes]
    return pd.DataFrame({
        "ot": [index * 1_800_000 for index in range(len(closes))],
        "o": closes,
        "h": [value + 0.4 for value in closes],
        "l": [value - 0.4 for value in closes],
        "c": closes,
        "v": [1000.0] * len(closes),
    })
```

Test the exact decision boundary by patching only the structure locator while leaving EWO and R math real:

```python
class PredictaExitEvidenceTest(unittest.TestCase):
    def test_long_requires_all_three_evidence_flags(self):
        candles = frame([100.0] * 35 + [110.0] * 10 + [102.0] * 5)
        with patch("predicta_exit.latest_confirmed_structure", return_value=("bottom", 103.0, 40)):
            evidence = evaluate_hard_exhaustion(
                candles, "LONG", 0, 100.0, 1.0, 10.4, PredictaExitRules()
            )
        self.assertTrue(evidence.ewo_reversed)
        self.assertTrue(evidence.progress_stalled)
        self.assertTrue(evidence.structure_broken)
        self.assertTrue(evidence.hard_exhaustion)

    def test_two_of_three_never_triggers(self):
        candles = frame([100.0] * 35 + [110.0] * 10 + [102.0] * 5)
        with patch("predicta_exit.latest_confirmed_structure", return_value=("bottom", 100.0, 40)):
            evidence = evaluate_hard_exhaustion(
                candles, "LONG", 0, 100.0, 1.0, 10.4, PredictaExitRules()
            )
        self.assertEqual(evidence.evidence_count, 2)
        self.assertFalse(evidence.hard_exhaustion)

    def test_short_zero_ewo_is_not_green(self):
        candles = frame([100.0] * 40)
        with patch("predicta_exit.latest_confirmed_structure", return_value=("top", 99.0, 36)):
            evidence = evaluate_hard_exhaustion(
                candles, "SHORT", 0, 100.0, 1.0, 2.0, PredictaExitRules()
            )
        self.assertEqual(evidence.ewo_value, 0.0)
        self.assertFalse(evidence.ewo_reversed)

    def test_runner_stop_never_loosens(self):
        rules = PredictaExitRules()
        self.assertEqual(runner_stop_price("LONG", 100.0, 2.0, 103.0, 110.0, 1.0, rules), 106.5)
        self.assertEqual(runner_stop_price("SHORT", 100.0, 2.0, 97.0, 90.0, 1.0, rules), 93.5)
```

Add unpatched long/short structure fixtures that pass through `_clean_kline_window()`. For LONG, use highs `[101, 100, 101, 103, 102]`, lows `[99, 98, 99, 101, 100]`, closes `[100, 99, 100, 102, 101]` and assert `("bottom", 98.0, 1)`. For SHORT, use highs `[101, 102, 101, 99, 100]`, lows `[99, 100, 99, 97, 98]`, closes `[100, 101, 100, 98, 99]` and assert `("top", 102.0, 1)`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_predicta_exit.py -v`

Expected: import failure because `predicta_exit.py` does not exist.

- [ ] **Step 3: Implement the immutable rules and evidence contracts**

Create these public dataclasses:

```python
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from screener import _clean_kline_window


@dataclass(frozen=True)
class PredictaExitRules:
    early_protect_r: float = 0.8
    early_lock_r: float = 0.0
    defense_r: float = 1.2
    defense_lock_r: float = 0.2
    stagnation_bars: int = 3
    min_mfe_improvement_r: float = 0.1
    min_drawdown_r: float = 0.5
    exhaustion_fraction: float = 0.5
    exhaustion_lock_r: float = 1.0
    target_r: float = 3.0
    target_fraction: float = 0.8
    runner_floor_r: float = 2.0
    atr_period: int = 14
    atr_mult: float = 3.5


@dataclass(frozen=True)
class ExhaustionEvidence:
    closed_bar_time: int = 0
    ewo_value: float | None = None
    ewo_reversed: bool = False
    mfe_before_window_r: float = 0.0
    mfe_improvement_r: float = 0.0
    drawdown_from_mfe_r: float = 0.0
    progress_stalled: bool = False
    fractal_type: str = ""
    fractal_price: float | None = None
    fractal_bar: int | None = None
    structure_broken: bool = False
    evidence_count: int = 0
    hard_exhaustion: bool = False
    available: bool = False
    reason: str = "insufficient_data"
```

- [ ] **Step 4: Implement strict post-entry structure and evidence math**

Implement `latest_confirmed_structure()` by cleaning containment relationships, scanning backward for a post-entry pivot, requiring the original strict confirmation close, and returning the raw structure boundary:

```python
def latest_confirmed_structure(candles: pd.DataFrame, direction: str, entry_time_ms: int):
    scoped = candles[pd.to_numeric(candles["ot"], errors="coerce") >= int(entry_time_ms)].reset_index(drop=True)
    if len(scoped) < 5:
        return None
    high, low, close, index_map = _clean_kline_window(
        scoped["h"].to_numpy(float), scoped["l"].to_numpy(float), scoped["c"].to_numpy(float)
    )
    for pivot in range(len(close) - 2, 0, -1):
        if direction == "LONG":
            if not (low[pivot] < low[pivot - 1] and low[pivot] < low[pivot + 1]):
                continue
            confirmations = [
                idx for idx in range(pivot + 1, len(close) - 1)
                if close[idx] > high[pivot]
                and np.min(low[pivot + 1:idx + 1]) >= low[pivot] * 0.997
            ]
            if confirmations:
                return "bottom", float(low[pivot]), int(index_map[pivot])
        else:
            if not (high[pivot] > high[pivot - 1] and high[pivot] > high[pivot + 1]):
                continue
            confirmations = [
                idx for idx in range(pivot + 1, len(close) - 1)
                if close[idx] < low[pivot]
                and np.max(high[pivot + 1:idx + 1]) <= high[pivot] * 1.003
            ]
            if confirmations:
                return "top", float(high[pivot]), int(index_map[pivot])
    return None
```

Implement `evaluate_hard_exhaustion()` with these exact rules:

```python
def evaluate_hard_exhaustion(
    candles: pd.DataFrame,
    direction: str,
    entry_time_ms: int,
    entry_price: float,
    initial_risk_per_unit: float,
    lifecycle_mfe_r: float,
    rules: PredictaExitRules,
) -> ExhaustionEvidence:
    required = max(35, rules.stagnation_bars + 1)
    if candles is None or len(candles) < required or initial_risk_per_unit <= 0:
        return ExhaustionEvidence()
    closed = candles.sort_values("ot").reset_index(drop=True)
    scoped = closed[pd.to_numeric(closed["ot"], errors="coerce") >= int(entry_time_ms)].reset_index(drop=True)
    if len(scoped) < rules.stagnation_bars + 1:
        return ExhaustionEvidence()
    ewo = closed["c"].rolling(5).mean() - closed["c"].rolling(35).mean()
    ewo_value = float(ewo.iloc[-1])
    ewo_reversed = ewo_value <= 0 if direction == "LONG" else ewo_value > 0
    favorable = (
        (scoped["h"] - entry_price) / initial_risk_per_unit
        if direction == "LONG"
        else (entry_price - scoped["l"]) / initial_risk_per_unit
    )
    before = favorable.iloc[:-rules.stagnation_bars]
    recent = favorable.iloc[-rules.stagnation_bars:]
    baseline = max(0.0, float(before.max()))
    improvement = max(0.0, float(recent.max()) - baseline)
    current_r = (
        (float(scoped["c"].iloc[-1]) - entry_price) / initial_risk_per_unit
        if direction == "LONG"
        else (entry_price - float(scoped["c"].iloc[-1])) / initial_risk_per_unit
    )
    drawdown = max(0.0, float(lifecycle_mfe_r) - current_r)
    stalled = improvement < rules.min_mfe_improvement_r and drawdown >= rules.min_drawdown_r
    structure = latest_confirmed_structure(closed, direction, entry_time_ms)
    fractal_type, fractal_price, fractal_bar = structure or ("", None, None)
    structure_broken = bool(
        fractal_price is not None and (
            float(scoped["c"].iloc[-1]) < fractal_price
            if direction == "LONG"
            else float(scoped["c"].iloc[-1]) > fractal_price
        )
    )
    count = int(ewo_reversed) + int(stalled) + int(structure_broken)
    return ExhaustionEvidence(
        closed_bar_time=int(float(closed["ot"].iloc[-1])), ewo_value=ewo_value,
        ewo_reversed=ewo_reversed, mfe_before_window_r=baseline,
        mfe_improvement_r=improvement, drawdown_from_mfe_r=drawdown,
        progress_stalled=stalled, fractal_type=fractal_type,
        fractal_price=fractal_price, fractal_bar=fractal_bar,
        structure_broken=structure_broken, evidence_count=count,
        hard_exhaustion=count == 3, available=True,
        reason="hard_exhaustion" if count == 3 else "evidence_incomplete",
    )
```

Add `runner_stop_price()` so the runner floor and chandelier always ratchet favorably:

```python
def runner_stop_price(direction, entry, risk, current_stop, extreme, atr, rules):
    sign = 1.0 if direction == "LONG" else -1.0
    floor = entry + sign * rules.runner_floor_r * risk
    chandelier = extreme - rules.atr_mult * atr if direction == "LONG" else extreme + rules.atr_mult * atr
    return max(current_stop, floor, chandelier) if direction == "LONG" else min(current_stop, floor, chandelier)
```

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_predicta_exit.py -v`

Expected: all tests pass, including `EWO=0` red/green boundary and 2-of-3 rejection.

- [ ] **Step 6: Commit the pure evaluator**

```powershell
git add predicta_exit.py tests/test_predicta_exit.py
git commit -m "feat: add Predicta exhaustion evaluator"
```

---

### Task 2: Version and Persist the V2 Position Contract

**Files:**
- Modify: `trader.py:82-263`
- Modify: `trader.py:1030-1075`
- Modify: `trader.py:4932-5057`
- Modify: `trader.py:5155-5210`
- Modify: `trader.py:5849-5898`
- Modify: `demo_bot_config.json`
- Create: `tests/test_predicta_exit_persistence.py`

**Interfaces:**
- Consumes: `TradeConfig.predicta_exit_profile_version` and an accepted real/paper entry fill.
- Produces: immutable V2 risk anchors and idempotency fields that survive save/load/exchange reconciliation; missing fields restore as V1.

- [ ] **Step 1: Add failing version-isolation and round-trip tests**

Use a temporary positions path and construct one V2 Predicta position with all stage fields populated. Assert:

```python
self.assertEqual(restored.exit_profile_version, 2)
self.assertEqual(restored.original_quantity, 10.0)
self.assertEqual(restored.initial_risk_per_unit, 2.0)
self.assertEqual(restored.initial_risk_usdt, 20.0)
self.assertTrue(restored.exhaustion_armed)
self.assertTrue(restored.exhaustion_partial_triggered)
self.assertEqual(restored.exhaustion_partial_quantity, 5.0)
self.assertEqual(restored.exhaustion_last_bar_time, 123)
self.assertEqual(restored.exit_pending_stage, "target_3r")
self.assertEqual(restored.exit_pending_order_id, "order-1")
self.assertTrue(restored.stop_refresh_pending)
```

Write a legacy JSON row with no new fields and assert it loads with `exit_profile_version == 1`, `original_quantity == 0.0`, and no V2 flags. Add an exchange-sync test that preserves every V2 field from `matched_local` while replacing only the actual remaining quantity.

- [ ] **Step 2: Run persistence tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_predicta_exit_persistence.py -v`

Expected: failures because the V2 config and position fields do not exist.

- [ ] **Step 3: Add demo-gated configuration with safe defaults**

Add these `TradeConfig` fields beside the existing Predicta and exit settings:

```python
predicta_exit_profile_version: int = 1
predicta_exhaustion_stagnation_bars: int = 3
predicta_exhaustion_min_mfe_improvement_r: float = 0.1
predicta_exhaustion_min_drawdown_r: float = 0.5
predicta_exhaustion_partial_fraction: float = 0.5
predicta_exhaustion_lock_r: float = 1.0
predicta_target_r: float = 3.0
predicta_target_close_fraction: float = 0.8
predicta_runner_floor_r: float = 2.0
```

Add only `"predicta_exit_profile_version": 2` to local `demo_bot_config.json`. Do not change the shared `trade_config.json`, add a UI field, or expose thresholds in Flask forms.

- [ ] **Step 4: Add V2 state to `Position` and JSON persistence**

Add fields with backward-compatible defaults:

```python
exit_profile_version: int = 1
original_quantity: float = 0.0
initial_risk_per_unit: float = 0.0
initial_risk_usdt: float = 0.0
exhaustion_armed: bool = False
exhaustion_partial_triggered: bool = False
exhaustion_partial_quantity: float = 0.0
target_3r_triggered: bool = False
target_3r_partial_quantity: float = 0.0
exhaustion_last_bar_time: int = 0
exit_pending_stage: str = ""
exit_pending_order_id: str = ""
exit_pending_expected_qty: float = 0.0
stop_refresh_pending: bool = False
```

Write every field in `_save_positions()`. Load absent values exactly as V1 defaults in `_load_positions()`. In `_sync_positions()`, copy these values only from `matched_local`; exchange-only orphan positions must be V1 because their original quantity and stage history cannot be proven.

- [ ] **Step 5: Freeze V2 risk only after the true entry fill**

In `_enter_key_candle_position()`, assign V2 only when `source_strategy == "predicta_ewo"`. In exchange mode, treat a non-zero order fill average or a matching `get_positions()` entry price/quantity as confirmed. If neither source confirms the fill, create the protected position as V1 and emit `predicta_v2_anchor_unavailable`; never label a reference signal price as a real fill. Paper mode uses the simulator fill directly.

```python
exit_version = int(getattr(self.cfg, "predicta_exit_profile_version", 1) or 1)
if source_strategy != "predicta_ewo":
    exit_version = 1
unit_risk = abs(float(fill_price) - float(sl_price))
pos.exit_profile_version = exit_version
if exit_version >= 2:
    pos.original_quantity = float(qty)
    pos.initial_risk_per_unit = unit_risk
    pos.initial_risk_usdt = unit_risk * float(qty)
    pos.risk_usdt = pos.initial_risk_usdt
```

Add the version and frozen risk anchors to the `entry_filled` event. Never recompute them after partial fills or exchange entry synchronization. During `_sync_positions()`, a matched V2 position retains its local frozen `entry_price`, `initial_sl`, original quantity, and risk anchors while only its current exchange quantity is refreshed. An unmatched exchange position remains V1.

- [ ] **Step 6: Run focused persistence and entry tests**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_predicta_exit_persistence.py tests/test_predicta_pipeline.py -v`

Expected: all tests pass; V1 rows stay V1, V2 survives restart, and only new Predicta entries can receive V2.

- [ ] **Step 7: Commit the persisted contract**

```powershell
git add trader.py demo_bot_config.json tests/test_predicta_exit_persistence.py tests/test_predicta_pipeline.py
git commit -m "feat: persist Predicta V2 exit state"
```

---

### Task 3: Implement Idempotent Live V2 Execution

**Files:**
- Modify: `trader.py:6656-7400`
- Create: `tests/test_predicta_live_exit.py`
- Test: `tests/test_binance_kline_routing.py`

**Interfaces:**
- Consumes: V2 `Position`, current exchange mark, closed mark-price candles, `ExhaustionEvidence`, and exchange order adapters.
- Produces: `_check_predicta_v2_exit(pos, df, current_price) -> str | None`, safe stage partials, stop replacements, and audit events. V1 continues through the existing `check_exit()` body unchanged.

- [ ] **Step 1: Add failing live state-machine tests**

Build a fake bot/client and add these exact test cases and assertions:

- `test_v2_does_not_start_atr_at_1_2r`: at 1.3R, assert `target_3r_triggered=False`, `highest_price` is unchanged, and no event reason contains `runner`.
- `test_live_3r_has_priority_over_same_cycle_exhaustion`: return hard evidence while mark price is 3R; assert the only market order quantity is 80% of current quantity and `exhaustion_partial_triggered=False`.
- `test_hard_exhaustion_closes_half_original_once_and_locks_1r`: start with original/current quantity 10; assert one 5-unit reduce-only order, remaining quantity 5, the exhaustion flag, and a 1R stop.
- `test_exhaustion_recovery_closes_80_percent_current_at_3r_leaving_ten_percent_original`: start with original 10/current 5 and the exhaustion flag; assert a 4-unit order and 1 unit remaining.
- `test_normal_3r_path_leaves_twenty_percent_original`: start with original/current 10; assert an 8-unit order and 2 units remaining.
- `test_runner_stop_is_max_of_2r_floor_and_long_chandelier`: set the current stop below both candidates; assert the larger candidate is selected and a later weaker candidate cannot lower it.
- `test_accepted_unreconciled_partial_is_not_resent_after_restart`: restore a pending order ID, make position reconciliation unavailable, and assert `market_order` is never called.
- `test_rejected_partial_does_not_set_stage_flag`: return an explicit order error and assert quantity, pending fields, and stage flags are unchanged.
- `test_wrong_side_1r_stop_is_skipped_without_market_exit`: put current mark below the long 1R price and assert no stop order is sent, no full-close reason is returned, and the skip audit is written.
- `test_v1_position_still_uses_legacy_tier2_and_trailing`: use version 1 and assert the existing `tier2_partial_r` path and current trailing mode still execute.

Patch `evaluate_hard_exhaustion()` to return explicit 2-of-3 or 3-of-3 evidence in execution tests. Keep Task 1 responsible for indicator math.

- [ ] **Step 2: Run focused live tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_predicta_live_exit.py -v`

Expected: failures because `check_exit()` has no V2 branch or pending-stage reconciliation.

- [ ] **Step 3: Add small helpers instead of duplicating the legacy partial block**

Add these exact methods to `SqueezeBreakoutBot`:

- `_predicta_exit_rules(self) -> PredictaExitRules`: map the V2 config values plus existing ATR period/multiplier into the immutable rules object.
- `_is_predicta_v2_position(self, pos: Position) -> bool`: require `source_strategy == "predicta_ewo"` and `exit_profile_version >= 2`.
- `_position_r(self, pos: Position, price: float) -> float`: compute direction-aware price R from `entry_price` and `initial_risk_per_unit`; raise `ValueError("position_invalid_risk")` for a non-positive anchor.
- `_favorable_stop(self, pos: Position, candidate: float) -> float`: return `max(current_sl, candidate)` for LONG and `min(current_sl, candidate)` for SHORT.
- `_stop_is_valid_now(self, pos: Position, stop: float, current_price: float) -> bool`: require LONG stop below mark and SHORT stop above mark.
- `_exchange_position_qty(self, symbol: str) -> float | None`: read the matching exchange position and return absolute quantity, `0.0` when definitively absent, or `None` on API failure.
- `_resolve_pending_exit_stage(self, pos: Position) -> bool`: reconcile accepted order state without submitting an order and return `True` only after local quantity/stage fields match the exchange.
- `_execute_v2_partial(self, pos: Position, planned_qty: float, current_price: float, stage: str) -> bool`: execute or reconcile one idempotent stage partial.
- `_replace_v2_stop(self, pos: Position, candidate: float, current_price: float) -> bool`: validate, favorably ratchet, cancel the prior stop, submit the new stop for actual remaining quantity, and persist retry state on failure.

`_execute_v2_partial()` must follow this order:

1. If `exit_pending_stage` is set, reconcile only; do not send another order.
2. Floor the planned quantity and determine whether the intended runner would be below exchange `minQty`; if so, close the full current quantity.
3. In paper mode, update quantity and append the trade immediately.
4. In exchange mode, submit one reduce-only market order.
5. On explicit rejection, leave all state unchanged.
6. On acceptance, persist `exit_pending_stage`, the returned order/tracking ID, and expected quantity before querying positions.
7. Reconcile the exchange remaining quantity; only then clear pending, update `pos.quantity`, set the stage flag/actual quantity, append the partial trade, and refresh the stop.

Every partial trade record must include `position_id=signal_key`, `exit_profile_version`, and `exit_stage` so later aggregation is deterministic. The eventual V2 final-close record must carry the same `position_id`; V1 records keep their current schema.

- [ ] **Step 4: Route V2 before the legacy exit chain**

In `check_exit()`, keep market-source fetching and exchange mark synchronization, then branch. Exchange synchronization may update `current_price`, `pnl`, and current quantity, but it must not overwrite a V2 position's frozen `entry_price` or risk anchors:

```python
if self._is_predicta_v2_position(pos):
    return self._check_predicta_v2_exit(pos, df, float(current_price))
```

Fetch the evidence frame with `closed_only=True`, `price_type="mark"`, and the existing exchange/market/testnet routing. Current R and the 3R trigger continue to use the real-time exchange mark, not the last closed candle.

- [ ] **Step 5: Implement the exact V2 priority order**

`_check_predicta_v2_exit()` must:

1. Resolve any pending accepted order and any pending stop refresh; return without new stage orders if reconciliation is incomplete.
2. Compute R from `initial_risk_per_unit`.
3. If 3R is reached and `target_3r_triggered` is false, close 80% of current quantity. After reconciliation, lock 2R and initialize runner extremes.
4. If 0.8R is reached, lock 0R and retain the existing three-cycle stop-check cooldown; if 1.2R is reached, lock 0.2R, refresh the three-cycle cooldown, and persist `exhaustion_armed=True`.
5. If armed, not exhausted, and 3R has not triggered, evaluate only a new `closed_bar_time`. Log all evidence fields. On 3-of-3, close 50% of `original_quantity`, capped by current quantity, then try to lock 1R.
6. If `target_3r_triggered` is true, update the ATR runner using closed ATR(14), real-time mark extrema, the 2R floor, and favorable-only ratcheting.
7. Run the existing PnL hard-stop and current stop-hit safety checks.

The V2 path must never read `tier2_partial_r`, start EMA trailing, or start ATR before `target_3r_triggered`.

- [ ] **Step 6: Add full audit payloads**

Append `predicta_exit_evidence`, `predicta_exit_partial`, `predicta_target_3r`, and `predicta_runner_stop` signal events. Evidence payloads include all fields from Section 7 of the design plus `exit_profile_version`, planned/actual quantities, before/after exchange quantities, order ID, and stop refresh result. Add `position_id=signal_key`, `exit_profile_version`, and `exit_stage="final"` to the V2 final trade record in `close_position()`.

- [ ] **Step 7: Run live, routing, and legacy regression tests**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_predicta_live_exit.py tests/test_binance_kline_routing.py tests/test_rj_time_stop.py -v`

Expected: all selected tests pass; mark-price routing remains intact and V1 outputs are unchanged.

- [ ] **Step 8: Commit live execution**

```powershell
git add trader.py tests/test_predicta_live_exit.py tests/test_binance_kline_routing.py
git commit -m "feat: execute Predicta V2 exits"
```

---

### Task 4: Mirror V2 in the Point-in-Time Replay Engine

**Files:**
- Modify: `strategy_core.py:50-90`
- Modify: `strategy_core.py:196-250`
- Modify: `backtest/engine.py`
- Modify: `backtest/metrics.py`
- Modify: `tests/test_strategy_core.py`
- Modify: `tests/test_backtest_system.py`

**Interfaces:**
- Consumes: the same `PredictaExitRules` and `evaluate_hard_exhaustion()` from Task 1, 1-minute execution bars, and only the 30-minute candles closed by the decision time.
- Produces: deterministic V2 `partial_exit`/`move_stop` events and grouped V2 comparison metrics without lookahead.

- [ ] **Step 1: Add failing replay state tests**

Extend `PositionState` test coverage for these state transitions:

```python
position = PositionState(
    "TESTUSDT", "LONG", 100.0, 98.0, 10.0,
    exit_profile_version=2, original_quantity=10.0,
)
```

Assert that a 1-minute high at 3R emits one `partial_exit` for 8.0 units, leaves 2.0, sets `target_3r_done`, and moves the stop to 2R. Create the exhaustion-recovery path with a 5.0-unit first partial, then assert the 3R action closes 4.0 and leaves 1.0. Add a same-bar test proving the adverse stop is still evaluated before optimistic intrabar target touches.

- [ ] **Step 2: Add a failing closed-30m exhaustion integration test**

Add `advance_predicta_closed_bar(position, candles, rules)` tests. Patch Task 1's evaluator to return hard evidence and assert:

```python
self.assertEqual([event.reason for event in events], ["predicta_hard_exhaustion"])
self.assertEqual(events[0].quantity, 5.0)
self.assertTrue(position.exhaustion_partial_done)
self.assertEqual(position.remaining_qty, 5.0)
```

Call it twice with the same last `ot` and assert the second call produces no event.

- [ ] **Step 3: Run focused replay tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_strategy_core.py tests/test_backtest_system.py -v`

Expected: failures because replay state lacks V2 fields and closed-candle evaluation.

- [ ] **Step 4: Extend replay state without changing V1 defaults**

Add to `PositionState`:

```python
exit_profile_version: int = 1
original_quantity: float = 0.0
initial_risk_per_unit: float = 0.0
exhaustion_armed: bool = False
exhaustion_partial_done: bool = False
target_3r_done: bool = False
exhaustion_last_bar_time: int = 0
highest_price: float = 0.0
lowest_price: float = 999999.0
```

Keep the current V1 branch of `advance_position()` unchanged. Add a V2 branch that applies adverse-first stops, 0.8R/1.2R protection, real-time 3R partials, and post-3R runner floors. Add `advance_predicta_closed_bar(position: PositionState, closed_candles: pd.DataFrame, rules: PredictaExitRules) -> tuple[PositionState, list[ExitDecision], ExhaustionEvidence | None]`. This function evaluates hard exhaustion once per new closed `ot`, emits the 50%-of-original partial at the latest close, and updates ATR trailing only if `target_3r_done` is true. When data are unavailable or the bar was already evaluated, it returns the unchanged position, an empty event list, and `None`.

When replay opens a V2 position, set `initial_risk_per_unit = abs(actual_sim_fill - decision.stop)` and write `risk_usdt = initial_risk_per_unit * quantity` into the entry event. Do not keep the requested cash-risk value when simulated slippage changes the real fill distance.

- [ ] **Step 5: Feed only point-in-time closed frames into existing positions**

In both `ReplayEngine` and `PortfolioReplayEngine`, process 1-minute bars up to each 30-minute decision time first. Then, before scanning new entries, call `advance_predicta_closed_bar()` for existing V2 positions with:

```python
closed_count = int(close_times[symbol].searchsorted(decision_time, side="right"))
closed = frames30[symbol].iloc[:closed_count].copy()
```

This ordering makes a 3R minute-price event win before the same boundary's exhaustion evaluation and prevents future candle access.

- [ ] **Step 6: Add V2 comparison metrics**

Keep grouping by `position_id`. Extend `summarize_positions()` with:

- `hard_exhaustion_trades`: number of grouped positions containing an exit reason of `predicta_hard_exhaustion`.
- `hard_exhaustion_recovered_3r`: number of those grouped positions whose lifecycle MFE reached at least 3R.
- `hard_exhaustion_recovery_rate`: recovered count divided by hard-exhaustion count, multiplied by 100 and rounded to four decimals; return `0.0` when the denominator is zero.
- `mean_mfe_giveback_r`: mean of `max(0, position_mfe_r - position_final_r)` across completed grouped positions, rounded to six decimals; return `0.0` for no completed positions.

Mark the grouped position from exit reasons rather than counting partial rows. Add tests with one exhaustion partial plus one 3R partial under the same `position_id` and assert it remains one trade.

- [ ] **Step 7: Run focused replay tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_strategy_core.py tests/test_backtest_system.py -v`

Expected: all tests pass; V1 fixtures remain unchanged, V2 ratios are exact, and no future 30-minute candle is read.

- [ ] **Step 8: Commit replay parity**

```powershell
git add strategy_core.py backtest/engine.py backtest/metrics.py tests/test_strategy_core.py tests/test_backtest_system.py
git commit -m "feat: replay Predicta V2 exits"
```

---

### Task 5: Group Live Trade Statistics by Original Position

**Files:**
- Create: `trade_metrics.py`
- Create: `tests/test_trade_metrics.py`
- Modify: `trader.py:8222-8640`

**Interfaces:**
- Consumes: live JSONL trade records containing `position_id` for V2 and legacy rows without it.
- Produces: `summarize_live_trades(records, today_key) -> dict` whose count and win rate treat all V2 partials/final exits as one original trade while preserving realized PnL as the sum of rows.

- [ ] **Step 1: Write failing aggregation tests**

Use records containing two partial rows and one final row with the same `position_id`, plus one legacy loss row. Assert:

```python
self.assertEqual(summary["total_trades"], 2)
self.assertEqual(summary["win_count"], 1)
self.assertEqual(summary["win_rate"], 50.0)
self.assertEqual(summary["realized_pnl"], 20.0)
self.assertEqual(summary["daily_trade_count"], 2)
```

Also assert two legacy rows without `position_id` remain two independent trades.

- [ ] **Step 2: Run aggregation tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_trade_metrics.py -v`

Expected: import failure because `trade_metrics.py` does not exist.

- [ ] **Step 3: Implement a pure grouping helper**

Use `position_id` when present; otherwise assign a unique row key so old data is not accidentally merged. Sum PnL for each group, use the earliest row date for daily count, and calculate win rate from grouped total PnL. Return realized PnL separately as the unchanged sum of every record.

```python
def summarize_live_trades(records: list[dict], today_key: str) -> dict:
    groups = {}
    realized = 0.0
    for index, record in enumerate(records):
        pnl = float(record.get("pnl", 0.0) or 0.0)
        realized += pnl
        key = str(record.get("position_id", "") or f"legacy-row-{index}")
        row = groups.setdefault(key, {"pnl": 0.0, "date": str(record.get("time", ""))[:10]})
        row["pnl"] += pnl
    wins = sum(item["pnl"] > 0 for item in groups.values())
    total = len(groups)
    return {
        "realized_pnl": round(realized, 2),
        "total_trades": total,
        "win_count": wins,
        "win_rate": round(wins * 100.0 / total, 1) if total else 0.0,
        "daily_trade_count": sum(item["date"] == today_key for item in groups.values()),
    }
```

- [ ] **Step 4: Use grouped counts in both fast and full status responses**

Before calling the helper in each status path, normalize each record to `{"position_id": record.get("position_id"), "time": record.get("time"), "pnl": self._trade_pnl_value(record)}` so exchange-reconciled PnL keeps the existing precedence. Replace only `trade_count`, `total_trades`, `win_rate`, `daily_trade_count`, and the nested accounting copies with the pure summary. Keep recent rows, equity reconciliation, and daily PnL behavior unchanged.

- [ ] **Step 5: Run metrics and status regressions**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_trade_metrics.py tests/test_backtest_system.py -v`

Expected: all tests pass and partial rows no longer inflate trade count or win rate.

- [ ] **Step 6: Commit grouped live statistics**

```powershell
git add trade_metrics.py trader.py tests/test_trade_metrics.py
git commit -m "fix: group partial exits as one trade"
```

---

### Task 6: Complete Local Verification

**Files:**
- Verify: `predicta_exit.py`
- Verify: `trade_metrics.py`
- Verify: `trader.py`
- Verify: `strategy_core.py`
- Verify: `backtest/`
- Verify: `tests/`

**Interfaces:**
- Consumes: all implementation commits.
- Produces: fresh compile, complete regression, point-in-time replay evidence, and a clean scoped diff.

- [ ] **Step 1: Compile every affected runtime module**

Run:

```powershell
python -m py_compile predicta_exit.py trade_metrics.py trader.py strategy_core.py backtest\engine.py backtest\metrics.py web_ui.py admin_server.py
```

Expected: exit code 0 with no output.

- [ ] **Step 2: Run the complete test suite**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m unittest discover -s tests -p 'test_*.py'`

Expected: all tests pass with zero failures or errors.

- [ ] **Step 3: Run a deterministic V1-versus-V2 portfolio replay**

Use the same frozen candles, entry decisions, initial equity, fees, slippage, and position slots for both profiles. Save or print only aggregate metrics: trades, mean/median R, 3R reach rate, hard-exhaustion count, hard-exhaustion recovery rate, profit factor, max drawdown R, and mean MFE giveback R.

Expected: both runs complete without lookahead errors. Report the measured values without claiming statistical advantage from an insufficient sample.

- [ ] **Step 4: Check scope, whitespace, and worktree state**

Run: `git diff --check; git status --short; git log -6 --oneline`

Expected: no whitespace errors and only intentional files/commits appear.

---

### Task 7: Back Up, Deploy Demo V2, Verify, and Record Progress

**Files:**
- Deploy: `predicta_exit.py` to `<deploy-dir>/predicta_exit.py`
- Deploy: `trade_metrics.py` to `<deploy-dir>/trade_metrics.py`
- Deploy: `trader.py` to `<deploy-dir>/trader.py`
- Deploy: `strategy_core.py` to `<deploy-dir>/strategy_core.py`
- Deploy: `backtest/engine.py` to `<deploy-dir>/backtest/engine.py`
- Deploy: `backtest/metrics.py` to `<deploy-dir>/backtest/metrics.py`
- Preserve and merge: `<deploy-dir>/demo_bot_config.json`
- Preserve: `<deploy-dir>/positions_<uid>.json`
- Modify locally after deployment: `PROGRESS.md`

**Interfaces:**
- Consumes: verified local commits, SSH key `<ssh-key>`, server `root@<production-host>`, and systemd services `macd-bot`/`macd-admin`.
- Produces: V2 enabled only for demo positions opened after deployment, unchanged existing positions, active services, matching hashes, and recorded deployment evidence.

- [ ] **Step 1: Capture a non-secret pre-deploy snapshot**

Record service states, demo running/source/mode, position symbols/quantities/entry times/version fields, active stop IDs, and only boolean API credential-presence flags. Do not print key or secret contents.

- [ ] **Step 2: Create a timestamped server backup**

Back up all runtime files being replaced plus `demo_bot_config.json` and `positions_<uid>.json` into `<deploy-dir>/backups/predicta_v2_exit_<timestamp>/`. Print the resolved backup path and file list before uploading.

- [ ] **Step 3: Upload runtime files only**

Upload the six runtime files to their matching server paths. Do not upload tests, specs, plans, `PROGRESS.md`, local configuration wholesale, or memory files.

- [ ] **Step 4: Merge only the demo version flag**

Run a server-side JSON merge that preserves every existing field and credential, changing only:

```json
{"predicta_exit_profile_version": 2}
```

Re-read and print only the new version, `entry_signal_source`, `predicta_choppy_filter_mode`, and credential-presence booleans.

- [ ] **Step 5: Compile and restart the trading service**

Run server-side Python compilation for all deployed modules, restart `macd-bot`, and query both services.

Expected: compilation succeeds and `macd-bot` plus `macd-admin` both report `active`.

- [ ] **Step 6: Verify version isolation and position continuity**

After restart, compare the pre/post position symbol, quantity, entry time, and stop ID snapshots. Existing positions must still have V1 semantics; the runtime config must report Predicta source and profile version 2. No position may be closed, resized, or migrated by deployment.

- [ ] **Step 7: Run non-ordering server probes**

Run a pure evaluator probe for both LONG and SHORT synthetic closed frames and a persistence reload probe. Verify EWO zero-color boundaries, 2-of-3 rejection, 3-of-3 hard exhaustion, V1 fallback, and V2 round-trip without sending exchange orders.

- [ ] **Step 8: Verify hashes and stop/order health**

Compare SHA256 for every deployed runtime file. Confirm current exchange positions have exactly one valid protective stop each or record and repair any pre-existing missing stop through the existing idempotent stop path.

- [ ] **Step 9: Update and commit `PROGRESS.md`**

Record: design/plan commits, implementation commits, exact tests and totals, replay outputs, backup path, deployed files/hashes, JSON merge action, service status, non-secret credential checks, pre/post position continuity, version-isolation proof, and server probe results.

```powershell
git add PROGRESS.md
git commit -m "docs: record Predicta V2 exit deployment"
```

- [ ] **Step 10: Perform final verification**

Re-run the complete local suite and compilation, `git diff --check`, `git status --short`, service status, runtime source/profile, position/stop continuity, and deployed hashes.

Expected: all tests pass, the local worktree is clean, both services are active, hashes match, demo V2 is enabled, user configs remain V1, and existing positions remain unchanged.
