# Momentum Compression Performance Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve exact momentum-compression strategy results while reducing a roughly 500-symbol production structure scan to no more than five minutes.

**Architecture:** Keep the scanner service and persistence flow unchanged. Optimize only the pure calculation module by proving safe EMA-based candidate pruning, preparing candles and indicators once for both directions, sharing direction-independent candidate geometry, and replacing the Python pivot loop with an equivalent NumPy implementation.

**Tech Stack:** Python 3.12, unittest, unittest.mock, Pandas, NumPy, Flask service integration, systemd.

## Global Constraints

- Preserve the rule order: select the maximal suffix satisfying every non-G01 structural rule, then apply `15 <= bars(W) <= 100`; never truncate to 100 bars.
- Preserve public function signatures and all evaluation, rejection, pool, event, snapshot, and alert fields.
- Do not change thresholds, scoring, state transitions, database contents, positions, orders, or user configuration.
- Do not add dependencies, process pools, or persistent calculation caches.
- A production scan over approximately 500 eligible symbols must complete in no more than five minutes.
- Local files must be committed before deployment; production deployment requires a timestamped backup and explicit user confirmation.
- After verified deployment, update local `PROGRESS.md` with the code change, deployed files, backup action, and verification result.

---

### Task 1: Lock down legacy suffix semantics and add exact EMA pruning

**Files:**
- Modify: `tests/test_momentum_compression.py`
- Modify: `momentum_compression.py:138-151`

**Interfaces:**
- Consumes: `_non_length_rules(frame: pd.DataFrame, side: str, params: CompressionParams) -> dict`.
- Produces: `_ema_candidate_start(indicators: pd.DataFrame, side: str) -> int` and an optimized `_maximal_structural_suffix(...)` with its existing public return type.

- [ ] **Step 1: Add the test-side legacy reference and pruning regression test**

Add `from unittest.mock import patch` and the following test helper near the existing frame helpers:

```python
def legacy_maximal_structural_suffix(indicators, side, params):
    if indicators.empty:
        return indicators, {"rejection_reasons": ["EMPTY_DATA"]}
    selected = indicators
    minimum = 2 * params.pivot_span + 1
    selected_rules = (
        _non_length_rules(indicators, side, params)
        if len(indicators) >= minimum
        else {"rejection_reasons": ["INSUFFICIENT_PIVOTS"]}
    )
    for start in range(len(indicators)):
        candidate = indicators.iloc[start:].reset_index(drop=True)
        if len(candidate) < minimum:
            continue
        rules = _non_length_rules(candidate, side, params)
        if not rules["rejection_reasons"]:
            return candidate, rules
    return selected, selected_rules
```

Add these tests to `CompressionRuleTests`:

```python
def test_ema_pruning_skips_only_suffixes_that_are_provably_invalid(self):
    indicators = add_compression_indicators(valid_compression_frame(220))
    indicators.loc[200, "ema8"] = indicators.loc[200, "ema21"]
    with patch("momentum_compression._non_length_rules", wraps=_non_length_rules) as rules:
        optimized_window, optimized_rules = _maximal_structural_suffix(
            indicators, "LONG", CompressionParams()
        )
    reference_window, reference_rules = legacy_maximal_structural_suffix(
        indicators, "LONG", CompressionParams()
    )
    candidate_lengths = [len(call.args[0]) for call in rules.call_args_list]
    self.assertFalse(any(20 <= length < 220 for length in candidate_lengths))
    self.assertEqual(list(optimized_window["ot"]), list(reference_window["ot"]))
    self.assertEqual(
        optimized_rules["rejection_reasons"],
        reference_rules["rejection_reasons"],
    )

def test_pruned_suffix_matches_legacy_for_fixed_random_samples(self):
    rng = np.random.default_rng(20260822)
    for side in ("LONG", "SHORT"):
        for bars in (14, 15, 40, 100, 101, 220):
            frame = valid_compression_frame(bars)
            frame["h"] += rng.normal(0.0, 0.05, bars)
            frame["l"] += rng.normal(0.0, 0.05, bars)
            indicators = add_compression_indicators(frame)
            actual_window, actual_rules = _maximal_structural_suffix(
                indicators, side, CompressionParams()
            )
            expected_window, expected_rules = legacy_maximal_structural_suffix(
                indicators, side, CompressionParams()
            )
            self.assertEqual(list(actual_window["ot"]), list(expected_window["ot"]))
            self.assertEqual(
                actual_rules["rejection_reasons"],
                expected_rules["rejection_reasons"],
            )
```

- [ ] **Step 2: Run the pruning regression and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionRuleTests.test_ema_pruning_skips_only_suffixes_that_are_provably_invalid -v
```

Expected: FAIL because the current implementation evaluates candidate lengths between 20 and 219.

- [ ] **Step 3: Implement the minimal exact pruning**

Add immediately before `_maximal_structural_suffix`:

```python
def _ema_candidate_start(indicators: pd.DataFrame, side: str) -> int:
    ema8 = indicators["ema8"].to_numpy(dtype=float, copy=False)
    ema21 = indicators["ema21"].to_numpy(dtype=float, copy=False)
    close = indicators["c"].to_numpy(dtype=float, copy=False)
    if side == "LONG":
        valid = (ema8 > ema21) & (close > np.maximum(ema8, ema21))
    else:
        valid = (ema8 < ema21) & (close < np.minimum(ema8, ema21))
    invalid = np.flatnonzero(~valid)
    return int(invalid[-1] + 1) if invalid.size else 0
```

Replace `_maximal_structural_suffix` with:

```python
def _maximal_structural_suffix(indicators, side, params, *, common_cache=None):
    if indicators.empty:
        return indicators, {"rejection_reasons": ["EMPTY_DATA"]}
    minimum = 2 * params.pivot_span + 1
    if len(indicators) < minimum:
        return indicators, {"rejection_reasons": ["INSUFFICIENT_PIVOTS"]}
    selected_rules = _non_length_rules(indicators, side, params)
    if not selected_rules["rejection_reasons"]:
        return indicators, selected_rules
    if "EMA_DISTANCE_TOO_WIDE" in selected_rules["rejection_reasons"]:
        return indicators, selected_rules
    first_start = max(1, _ema_candidate_start(indicators, side))
    for start in range(first_start, len(indicators) - minimum + 1):
        candidate = indicators.iloc[start:]
        rules = _non_length_rules(candidate, side, params)
        if not rules["rejection_reasons"]:
            return candidate, rules
    return indicators, selected_rules
```

The `common_cache` keyword is introduced here for Task 3; it is unused until that task and does not alter callers.

- [ ] **Step 4: Run focused and full rule tests**

Run:

```powershell
python -m unittest tests.test_momentum_compression -v
```

Expected: all momentum-compression rule tests PASS, including both new legacy-equivalence tests.

- [ ] **Step 5: Commit Task 1**

```powershell
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "perf: prune invalid compression suffixes"
```

---

### Task 2: Prepare closed candles and indicators once for both sides

**Files:**
- Modify: `tests/test_momentum_compression.py`
- Modify: `momentum_compression.py:206-262`

**Interfaces:**
- Consumes: `add_compression_indicators(frame)`, `_maximal_structural_suffix(...)`, `_rejected(...)`.
- Produces: `_prepare_evaluation_frame(closed_15m, evaluated_at_ms) -> tuple[pd.DataFrame, pd.DataFrame | None, list[str]]` and `_evaluate_prepared_side(...) -> dict`.

- [ ] **Step 1: Add a failing single-preparation test**

Add `import momentum_compression as compression_module` and this test:

```python
def test_evaluate_both_sides_prepares_indicators_once(self):
    frame = valid_compression_frame(40)
    evaluated_at = int(frame["ot"].iloc[-1] + 900_000)
    with patch(
        "momentum_compression.add_compression_indicators",
        wraps=compression_module.add_compression_indicators,
    ) as indicators:
        rows = evaluate_both_sides(
            "TESTUSDT", frame, 115.0,
            evaluated_at_ms=evaluated_at,
            htf_alignment_by_side={"LONG": "UNKNOWN", "SHORT": "UNKNOWN"},
        )
    self.assertEqual([row["side"] for row in rows], ["LONG", "SHORT"])
    self.assertEqual(indicators.call_count, 1)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionRuleTests.test_evaluate_both_sides_prepares_indicators_once -v
```

Expected: FAIL with `2 != 1`.

- [ ] **Step 3: Extract shared preparation without changing validation order**

Add the exact preparation helper below:

```python
def _prepare_evaluation_frame(
    closed_15m: pd.DataFrame,
    evaluated_at_ms: int,
) -> tuple[pd.DataFrame, pd.DataFrame | None, list[str]]:
    frame = closed_15m.loc[:, REQUIRED_COLUMNS].copy().reset_index(drop=True)
    frame["ot"] = pd.to_numeric(frame["ot"], errors="coerce")
    frame = frame[frame["ot"] + 900_000 <= evaluated_at_ms].reset_index(drop=True)
    if frame.empty:
        return frame, None, ["NO_CLOSED_CANDLES"]
    for column in REQUIRED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not np.isfinite(frame.to_numpy(dtype=float)).all():
        return frame, None, ["NONFINITE_OHLCV"]
    return frame, add_compression_indicators(frame), []
```

Add `_evaluate_prepared_side` with this complete body:

```python
def _evaluate_prepared_side(
    symbol: str,
    side: str,
    frame: pd.DataFrame,
    indicators: pd.DataFrame,
    live_price: float,
    *,
    evaluated_at_ms: int,
    htf_alignment: str,
    params: CompressionParams,
    common_cache: dict | None = None,
) -> dict:
    window, metrics = _maximal_structural_suffix(
        indicators, side, params, common_cache=common_cache
    )
    bars = len(window)
    reasons = list(metrics.get("rejection_reasons", []))
    if bars < params.min_bars:
        reasons.append("WINDOW_TOO_SHORT")
    if bars > params.max_bars:
        reasons.append("WINDOW_TOO_LONG")
    envelope = metrics.get("envelope", {})
    if reasons:
        return _rejected(
            symbol, side, evaluated_at_ms, htf_alignment,
            reasons, params, bars, window,
        )
    upper, lower = envelope["upper"], envelope["lower"]
    atr14 = float(window["atr14"].iloc[-1])
    metrics["ema_distance_atr"] = (
        abs(float(window["ema8"].iloc[-1] - window["ema21"].iloc[-1])) / atr14
        if atr14 else float("inf")
    )
    score, score_components = _quality_score(metrics, params)
    result = {
        "symbol": symbol, "side": side, "evaluated_at": evaluated_at_ms,
        "htf_alignment": htf_alignment, "parameter_version": params.version,
        "rejection_reasons": [], "compression_bars": bars,
        "compression_start_time": int(window["ot"].iloc[0]),
        "compression_end_time": int(window["ot"].iloc[-1]),
        "upper_boundary_price": float(upper.iloc[-1]),
        "lower_boundary_price": float(lower.iloc[-1]),
        "upper_boundary_slope": envelope["upper_slope"],
        "lower_boundary_slope": envelope["lower_slope"],
        "atr14": atr14, "ema8": float(window["ema8"].iloc[-1]),
        "ema21": float(window["ema21"].iloc[-1]),
        "ema_distance_atr": metrics["ema_distance_atr"],
        "contraction_ratio": metrics["contraction_ratio"],
        "pivot_high_count": len(metrics["pivot_highs"]),
        "pivot_low_count": len(metrics["pivot_lows"]),
        "directional_touch_count": len(metrics["directional_events"]),
        "directional_touch_times": [
            int(window["ot"].iloc[index]) for index in metrics["directional_events"]
        ],
        "breakout_buffer_price": atr14 * params.breakout_buffer_atr,
        "quality_score": score, "score_components": score_components,
        "swing": metrics["swing"],
    }
    result["compression_id"] = compression_identity(result)
    result["state"] = _classify_without_episode(result, float(live_price), params)
    return result
```

Replace `evaluate_side` with:

```python
def evaluate_side(symbol, side, closed_15m, live_price, *, evaluated_at_ms,
                  htf_alignment, params=CompressionParams()):
    reasons = []
    if side not in ("LONG", "SHORT"):
        reasons.append("INVALID_SIDE")
    if not isinstance(live_price, (int, float, np.number)) or not math.isfinite(float(live_price)):
        reasons.append("INVALID_LIVE_PRICE")
    if closed_15m is None or closed_15m.empty:
        reasons.append("EMPTY_DATA")
    elif any(column not in closed_15m.columns for column in REQUIRED_COLUMNS):
        reasons.append("MISSING_REQUIRED_COLUMNS")
    if reasons:
        return _rejected(symbol, side, evaluated_at_ms, htf_alignment, reasons, params)
    frame, indicators, preparation_reasons = _prepare_evaluation_frame(
        closed_15m, evaluated_at_ms
    )
    if preparation_reasons:
        return _rejected(
            symbol, side, evaluated_at_ms, htf_alignment,
            preparation_reasons, params, len(frame), frame,
        )
    return _evaluate_prepared_side(
        symbol, side, frame, indicators, live_price,
        evaluated_at_ms=evaluated_at_ms, htf_alignment=htf_alignment,
        params=params,
    )
```

Replace `evaluate_both_sides` with:

```python
def evaluate_both_sides(symbol, closed_15m, live_price, *, evaluated_at_ms,
                        htf_alignment_by_side, params=CompressionParams()):
    reasons = []
    if not isinstance(live_price, (int, float, np.number)) or not math.isfinite(float(live_price)):
        reasons.append("INVALID_LIVE_PRICE")
    if closed_15m is None or closed_15m.empty:
        reasons.append("EMPTY_DATA")
    elif any(column not in closed_15m.columns for column in REQUIRED_COLUMNS):
        reasons.append("MISSING_REQUIRED_COLUMNS")
    if reasons:
        return [
            _rejected(
                symbol, side, evaluated_at_ms,
                htf_alignment_by_side.get(side, "UNKNOWN"), reasons, params,
            )
            for side in ("LONG", "SHORT")
        ]
    frame, indicators, preparation_reasons = _prepare_evaluation_frame(
        closed_15m, evaluated_at_ms
    )
    if preparation_reasons:
        return [
            _rejected(
                symbol, side, evaluated_at_ms,
                htf_alignment_by_side.get(side, "UNKNOWN"),
                preparation_reasons, params, len(frame), frame,
            )
            for side in ("LONG", "SHORT")
        ]
    common_cache = {}
    return [
        _evaluate_prepared_side(
            symbol, side, frame, indicators, live_price,
            evaluated_at_ms=evaluated_at_ms,
            htf_alignment=htf_alignment_by_side.get(side, "UNKNOWN"),
            params=params, common_cache=common_cache,
        )
        for side in ("LONG", "SHORT")
    ]
```

- [ ] **Step 4: Verify the shared path and public compatibility**

Run:

```powershell
python -m unittest tests.test_momentum_compression tests.test_momentum_compression_service -v
```

Expected: all tests PASS; the new test reports one indicator preparation.

- [ ] **Step 5: Commit Task 2**

```powershell
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "perf: share compression indicator preparation"
```

---

### Task 3: Share candidate geometry and vectorize pivot discovery

**Files:**
- Modify: `tests/test_momentum_compression.py`
- Modify: `momentum_compression.py:42-135`

**Interfaces:**
- Consumes: the `common_cache` dictionary passed by Task 2.
- Produces: `_common_structure(frame, params) -> dict`; `_non_length_rules(..., common=None)`; vectorized `_pivots` with the existing return type.

- [ ] **Step 1: Add pivot equivalence and geometry-sharing tests**

Add this test-side pivot reference:

```python
def legacy_pivots(frame, span):
    highs, lows = [], []
    for index in range(span, len(frame) - span):
        high_window = frame["h"].iloc[index - span:index + span + 1]
        low_window = frame["l"].iloc[index - span:index + span + 1]
        if frame["h"].iloc[index] == high_window.max() and (high_window == frame["h"].iloc[index]).sum() == 1:
            highs.append(index)
        if frame["l"].iloc[index] == low_window.min() and (low_window == frame["l"].iloc[index]).sum() == 1:
            lows.append(index)
    return highs, lows
```

Add:

```python
def test_vectorized_pivots_match_legacy_for_unique_and_tied_extrema(self):
    rng = np.random.default_rng(20260822)
    for bars in (5, 15, 40, 220):
        frame = valid_compression_frame(bars)
        frame["h"] += rng.normal(0.0, 0.1, bars)
        frame["l"] += rng.normal(0.0, 0.1, bars)
        if bars >= 15:
            frame.loc[7, "h"] = frame.loc[8, "h"] = max(frame.loc[7, "h"], frame.loc[8, "h"])
            frame.loc[10, "l"] = frame.loc[11, "l"] = min(frame.loc[10, "l"], frame.loc[11, "l"])
        self.assertEqual(_pivots(frame, 2), legacy_pivots(frame, 2))

def test_both_sides_share_direction_independent_candidate_geometry(self):
    frame = valid_compression_frame(40)
    evaluated_at = int(frame["ot"].iloc[-1] + 900_000)
    with patch(
        "momentum_compression._common_structure",
        wraps=compression_module._common_structure,
    ) as common:
        evaluate_both_sides(
            "TESTUSDT", frame, 115.0,
            evaluated_at_ms=evaluated_at,
            htf_alignment_by_side={},
        )
    starts = [int(call.args[0]["ot"].iloc[0]) for call in common.call_args_list]
    self.assertEqual(len(starts), len(set(starts)))
```

The second test initially errors because `_common_structure` does not exist; this is the required RED state.

- [ ] **Step 2: Run the geometry-sharing test and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionRuleTests.test_both_sides_share_direction_independent_candidate_geometry -v
```

Expected: ERROR with `AttributeError: module 'momentum_compression' has no attribute '_common_structure'`.

- [ ] **Step 3: Vectorize `_pivots`**

Replace its loop with:

```python
def _pivots(frame: pd.DataFrame, span: int) -> tuple[list[int], list[int]]:
    width = 2 * span + 1
    if span < 0 or len(frame) < width:
        return [], []
    highs = frame["h"].to_numpy(dtype=float, copy=False)
    lows = frame["l"].to_numpy(dtype=float, copy=False)
    high_windows = np.lib.stride_tricks.sliding_window_view(highs, width)
    low_windows = np.lib.stride_tricks.sliding_window_view(lows, width)
    high_centers = highs[span:span + len(high_windows)]
    low_centers = lows[span:span + len(low_windows)]
    high_mask = (high_centers == high_windows.max(axis=1)) & (
        (high_windows == high_centers[:, None]).sum(axis=1) == 1
    )
    low_mask = (low_centers == low_windows.min(axis=1)) & (
        (low_windows == low_centers[:, None]).sum(axis=1) == 1
    )
    return (
        (np.flatnonzero(high_mask) + span).tolist(),
        (np.flatnonzero(low_mask) + span).tolist(),
    )
```

- [ ] **Step 4: Extract and cache common candidate geometry**

Add:

```python
def _common_structure(frame: pd.DataFrame, params: CompressionParams) -> dict:
    pivot_highs, pivot_lows = _pivots(frame, params.pivot_span)
    envelope = _fit_shifted_envelope(frame, pivot_highs, pivot_lows)
    if not envelope:
        return {
            "pivot_highs": pivot_highs,
            "pivot_lows": pivot_lows,
            "envelope": {},
        }
    widths = envelope["upper"] - envelope["lower"]
    contraction_ratio = (
        float(widths.iloc[-1] / widths.iloc[0])
        if widths.iloc[0] else float("inf")
    )
    return {
        "pivot_highs": pivot_highs,
        "pivot_lows": pivot_lows,
        "envelope": envelope,
        "contraction_ratio": contraction_ratio,
    }
```

Replace `_non_length_rules` with the exact ordering-preserving implementation:

```python
def _non_length_rules(frame, side, params, *, common=None):
    common = common if common is not None else _common_structure(frame, params)
    pivot_highs = common["pivot_highs"]
    pivot_lows = common["pivot_lows"]
    envelope = common["envelope"]
    reasons = []
    if not envelope:
        reasons.append("INSUFFICIENT_PIVOTS")
        return {
            "rejection_reasons": reasons,
            "pivot_highs": pivot_highs,
            "pivot_lows": pivot_lows,
            "envelope": envelope,
        }
    upper, lower = envelope["upper"], envelope["lower"]
    atr = frame["atr14"].replace(0, np.nan)
    ema8, ema21 = frame["ema8"], frame["ema21"]
    if side == "LONG":
        if not bool((ema8 > ema21).all()):
            reasons.append("EMA_DIRECTION")
        if not bool((frame["c"] > pd.concat((ema8, ema21), axis=1).max(axis=1)).all()):
            reasons.append("CLOSE_IN_EMA_BAND")
        directional_events = _touch_events(
            frame["l"], lower, atr, params.touch_tolerance_atr
        )
    else:
        if not bool((ema8 < ema21).all()):
            reasons.append("EMA_DIRECTION")
        if not bool((frame["c"] < pd.concat((ema8, ema21), axis=1).min(axis=1)).all()):
            reasons.append("CLOSE_IN_EMA_BAND")
        directional_events = _touch_events(
            frame["h"], upper, atr, params.touch_tolerance_atr
        )
    if (
        not math.isfinite(float(atr.iloc[-1]))
        or abs(float(ema8.iloc[-1] - ema21.iloc[-1]))
        > float(atr.iloc[-1]) * params.max_ema_distance_atr
    ):
        reasons.append("EMA_DISTANCE_TOO_WIDE")
    swing = _swing_structure(frame, pivot_highs, pivot_lows, side)
    if not swing["valid"]:
        reasons.append("INVALID_SWING_STRUCTURE")
    contraction_ratio = common["contraction_ratio"]
    if contraction_ratio > params.contraction_ratio_max:
        reasons.append("INSUFFICIENT_CONTRACTION")
    if len(directional_events) < params.min_directional_boundary_touches:
        reasons.append("INSUFFICIENT_DIRECTIONAL_TOUCHES")
    return {
        "rejection_reasons": reasons,
        "pivot_highs": pivot_highs,
        "pivot_lows": pivot_lows,
        "envelope": envelope,
        "directional_events": directional_events,
        "swing": swing,
        "contraction_ratio": contraction_ratio,
    }
```

Replace `_maximal_structural_suffix` with the complete cache-aware version:

```python
def _maximal_structural_suffix(
    indicators, side, params, *, common_cache=None,
):
    if indicators.empty:
        return indicators, {"rejection_reasons": ["EMPTY_DATA"]}
    minimum = 2 * params.pivot_span + 1
    if len(indicators) < minimum:
        return indicators, {"rejection_reasons": ["INSUFFICIENT_PIVOTS"]}
    cache = common_cache if common_cache is not None else {}

    def rules_for(candidate):
        candidate_length = len(candidate)
        common = cache.get(candidate_length)
        if common is None:
            common = _common_structure(candidate, params)
            cache[candidate_length] = common
        return _non_length_rules(candidate, side, params, common=common)

    selected_rules = rules_for(indicators)
    if not selected_rules["rejection_reasons"]:
        return indicators, selected_rules
    if "EMA_DISTANCE_TOO_WIDE" in selected_rules["rejection_reasons"]:
        return indicators, selected_rules
    first_start = max(1, _ema_candidate_start(indicators, side))
    for start in range(first_start, len(indicators) - minimum + 1):
        candidate = indicators.iloc[start:]
        rules = rules_for(candidate)
        if not rules["rejection_reasons"]:
            return candidate, rules
    return indicators, selected_rules
```

Use `rules_for` for the full window and every candidate. Because both directions share one fixed-end indicator frame, candidate length uniquely identifies its start and allows geometry reuse without coupling the selected LONG and SHORT windows.

- [ ] **Step 5: Run equivalence and integration tests**

Run:

```powershell
python -m unittest tests.test_momentum_compression tests.test_momentum_compression_service tests.test_momentum_compression_store -v
```

Expected: all tests PASS, including pivot equivalence, suffix equivalence, one-time indicator preparation, and unique common-geometry start times.

- [ ] **Step 6: Commit Task 3**

```powershell
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "perf: reuse compression candidate geometry"
```

---

### Task 4: Measure the optimized calculation and complete local verification

**Files:**
- Create: `tests/benchmark_momentum_compression.py`
- Modify only if a verified equivalence or performance failure requires a surgical correction: `momentum_compression.py`

**Interfaces:**
- Consumes: `evaluate_both_sides(...)` and the existing deterministic test frame construction.
- Produces: a reproducible offline timing command with a nonzero exit status when 500 evaluations exceed 300 seconds.

- [ ] **Step 1: Add the deterministic offline benchmark**

Create:

```python
import sys
import time

import numpy as np
import pandas as pd

from momentum_compression import evaluate_both_sides


BASE_OT = 1_700_000_000_000


def frame_for(seed, bars=220):
    rng = np.random.default_rng(seed)
    indexes = np.arange(bars, dtype=float)
    pulse = np.sin(indexes * np.pi / 3) + rng.normal(0.0, 0.03, bars)
    return pd.DataFrame({
        "ot": BASE_OT + indexes.astype(int) * 900_000,
        "o": 100.0 + 0.4 * indexes,
        "h": 105.0 + 0.3 * indexes + pulse,
        "l": 95.0 + 0.5 * indexes + pulse,
        "c": 100.0 + 0.4 * indexes,
        "v": np.full(bars, 1000.0),
    })


def main():
    frames = [frame_for(seed) for seed in range(500)]
    evaluated_at = int(frames[0]["ot"].iloc[-1] + 900_000)
    started = time.perf_counter()
    for index, frame in enumerate(frames):
        evaluate_both_sides(
            f"TEST{index}USDT", frame, float(frame["c"].iloc[-1]),
            evaluated_at_ms=evaluated_at,
            htf_alignment_by_side={},
        )
    elapsed = time.perf_counter() - started
    print(f"500-symbol offline calculation: {elapsed:.3f}s")
    return 0 if elapsed <= 300.0 else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the offline benchmark**

Run:

```powershell
python tests/benchmark_momentum_compression.py
```

Expected: exit code 0; the printed numeric duration is no greater than 300 seconds.

- [ ] **Step 3: Run full verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m py_compile momentum_compression.py momentum_compression_service.py momentum_compression_monitor.py web_ui.py admin_server.py
git diff --check
```

Expected: all tests PASS, compilation exits 0, and `git diff --check` prints no errors.

- [ ] **Step 4: Commit the benchmark and any verified correction**

```powershell
git add tests/benchmark_momentum_compression.py momentum_compression.py tests/test_momentum_compression.py
git commit -m "test: benchmark compression scan calculation"
```

---

### Task 5: Production deployment, live scan verification, and bookkeeping

**Files:**
- Deploy: `momentum_compression.py`
- Modify after verified deployment: `PROGRESS.md`

**Interfaces:**
- Consumes: committed and locally verified calculation module.
- Produces: a backed-up production deployment, one completed live scan report, and a local deployment record.

- [ ] **Step 1: Present the exact production action and obtain explicit confirmation**

State that deployment will back up and replace only `<deploy-dir>/momentum_compression.py`, restart `macd-bot`, preserve databases/configuration/positions/orders, and then run one real scan. Do not continue without confirmation.

- [ ] **Step 2: Create and verify the production backup**

After confirmation, resolve the target directory and create:

```bash
cd <deploy-dir>
backup_dir="backups/compression_performance_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$backup_dir"
cp -a momentum_compression.py "$backup_dir/"
test -s "$backup_dir/momentum_compression.py"
printf '%s\n' "$backup_dir"
```

Expected: an explicit backup path and exit code 0.

- [ ] **Step 3: Upload the committed module and restart only the user service**

Upload the local committed `momentum_compression.py` to `<deploy-dir>/momentum_compression.py`, then run:

```bash
cd <deploy-dir>
python3 -m py_compile momentum_compression.py
systemctl restart macd-bot
systemctl is-active macd-bot
```

Expected: compilation exits 0 and service state is `active`.

- [ ] **Step 4: Trigger or wait for one full live scan and verify the five-minute contract**

Poll `http://127.0.0.1:5000/internal/compression/status` at intervals shorter than 60 seconds while keeping the user updated. Success requires one newly completed report satisfying all of:

```text
structure_scanning = false
scan_overdue = false
0 < scan.scanned
monitor.scan_duration_ms <= 300000
monitor.last_error contains no unreported worker failure
```

Record the exact `scanned`, `eligible`, `errors`, `scan_duration_ms`, start time, and finish time. Do not interpret `eligible=0` until these completion conditions hold.

- [ ] **Step 5: Roll back on failed production acceptance**

If the service fails, the scan exceeds five minutes, or completion fields are inconsistent, copy the backed-up `momentum_compression.py` over the deployed file, run `python3 -m py_compile momentum_compression.py`, restart `macd-bot`, and report the failed acceptance evidence. Do not alter state JSON, snapshots, databases, positions, orders, or configuration.

- [ ] **Step 6: Update and commit `PROGRESS.md` after successful deployment**

Add a dated entry containing the four verified facts below. Write the literal backup path and numeric scan results obtained in Steps 2 and 4; do not copy example placeholders into `PROGRESS.md`:

```markdown
### 2026-08-22 动能压缩扫描等价性能优化

- 变更：EMA 硬条件候选剪枝、双方向共享指标准备与候选几何、NumPy 枢轴计算；策略参数和最大连续结构后缀语义未变。
- 部署：`momentum_compression.py` 上传至 `<deploy-dir>/`，重启 `macd-bot`。
- 备份：服务器步骤2输出的完整备份目录。
- 验证：写入状态接口返回的 `scanned`、`eligible`、`errors`、`scan_duration_ms` 原始数值；服务为 active，扫描未超时。
```

After recording the exact verified production values, run:

```powershell
git add PROGRESS.md
git commit -m "docs: record compression performance deployment"
git status --short
```

Expected: commit succeeds and the worktree is clean.
