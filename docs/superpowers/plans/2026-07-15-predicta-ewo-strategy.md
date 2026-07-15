# Predicta + EWO Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independent `predicta_ewo` signal source that hard-rejects choppy Predicta BUY/SELL signal candles, enters immediately after the signal close when EWO is already aligned, otherwise confirms within six closed bars using price breakout plus an EWO turn, and reuses existing risk and non-time-based exits.

**Architecture:** Put Pine-compatible indicator math and the deterministic six-bar setup state machine in a new pure module. Keep exchange scanning, candidate persistence, order placement, status events, and source routing in `trader.py`; adapt the shared replay contract so live and historical decisions call the same pure functions. Preserve RJ through wrappers and source-specific routing rather than renaming or overwriting it.

**Tech Stack:** Python 3.12, pandas, NumPy, Flask, SQLite-backed user config, native HTML/JavaScript, `unittest`/pytest, existing event-driven 30m/1m replay engine.

## Global Constraints

- Add `entry_signal_source=predicta_ewo` and `source_strategy=predicta_ewo`; do not delete or overwrite `rj_only`.
- Use only closed candles for signal creation, setup confirmation, invalidation, and EWO.
- Predicta V1 constants are EMA 8/21, Supertrend ATR 10 with factor 3.0, and original OHLCV-derived Delta.
- Default confirmation is six bars, EWO SMA 5/35, ATR breakout buffer 0.08, hard choppy filtering, stop buffer 0.5 ATR, minimum stop 0.3%, maximum stop 8%.
- Do not use Perfect Time, Prediction percentage, eight-point confluence, near-close, touch, or hold confirmation.
- A breakout with wrong-sign EWO remains pending through bar six; bar seven cannot confirm.
- Same-sign signal-bar EWO uses the fast path and never enters the candidate pool; opposite-sign or zero EWO uses the six-bar path.
- Fast-path live orders are submitted only after the signal bar closes; replay fills at the next available 1m open, never the signal-bar close.
- Fast-path rejection does not fall back into the candidate pool.
- Fast-path stop and breakout-free decision use signal-bar ATR; waiting-path breakout buffer and stop use confirmation-bar ATR.
- `predicta_ewo` positions skip every time-stop branch but retain initial stop, 0.8R protection, 1.2R defense, partial exit, EMA/ATR trailing, stop-order persistence, and real-PnL reconciliation.
- Preserve the existing BTC direction gate, risk limits, maximum positions, signal deduplication, and failed-order cooldown.
- No new frontend framework or npm dependency; do not change existing IDs except by adding new Predicta-specific controls.
- No production deployment in this plan. Update `PROGRESS.md` only after a later explicitly authorized and verified server deployment.

---

## File Map

- Create `predicta_indicator.py`: pure Predicta lines, EWO, signal construction, and setup evaluation.
- Create `tests/test_predicta_indicator.py`: Pine parity and deterministic setup-state tests.
- Create `tests/test_predicta_pipeline.py`: bot integration, hard choppy rejection, pool lifecycle, entry source, and time-stop exemption.
- Create `tests/test_predicta_config.py`: config load/save and admin control coverage.
- Create `backtest_data/predicta_ewo_frozen_rules.json`: immutable first replay declaration.
- Modify `trader.py`: config, source routing, Predicta scan/pool, shared key-candle entry, time-stop exemption, status fields.
- Modify `strategy_core.py`: source-neutral entry router with Predicta adapter.
- Modify `backtest/engine.py`: call the source-neutral entry router and remove RJ-only prefilter assumptions for Predicta runs.
- Modify `backtest/cli.py`: precompute the configured source instead of hard-coding RJ indicators.
- Modify `tests/test_strategy_core.py`: live/replay Predicta parity and closed-candle guards.
- Modify `tests/test_backtest_system.py`: next-minute fill, dedupe, and portfolio-slot behavior for Predicta.
- Modify `admin_server.py`: add source option and Predicta-specific inputs/save payload.
- Modify `demo_bot_config.json`: declare Predicta defaults and select the new source locally.
- Modify `CLAUDE.md`: document the new independent signal and exit exception.

---

### Task 1: Pine-Compatible Predicta and EWO Core

**Files:**
- Create: `predicta_indicator.py`
- Create: `tests/test_predicta_indicator.py`

**Interfaces:**
- Produces: `PredictaParams`, `PredictaSetupDecision`, `compute_predicta(df, params)`, `make_predicta_setup(symbol, direction, interval, frame, signal_index, signal_ewo, choppy_state, params)`, and `evaluate_predicta_setup(setup, df, params, atr_value)`.
- Consumes: DataFrames with `o`, `h`, `l`, `c`, `v`, and `ot` columns.

- [ ] **Step 1: Write failing tests for exact signal formulas**

```python
# tests/test_predicta_indicator.py
import unittest
import numpy as np
import pandas as pd

from predicta_indicator import PredictaParams, compute_predicta


def frame(closes):
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "ot": np.arange(len(close), dtype=np.int64) * 1_800_000,
        "o": close - 0.1,
        "h": close + 0.4,
        "l": close - 0.4,
        "c": close,
        "v": np.full(len(close), 1000.0),
    })


class PredictaIndicatorTest(unittest.TestCase):
    def test_delta_matches_original_close_location_formula(self):
        df = frame(np.linspace(90, 110, 50))
        result = compute_predicta(df, PredictaParams())
        expected = df["v"] * ((df["c"] - df["l"]) - (df["h"] - df["c"])) / (df["h"] - df["l"])
        pd.testing.assert_series_equal(result["delta"], expected, check_names=False)

    def test_ewo_is_sma5_minus_sma35(self):
        df = frame(np.arange(1, 61))
        result = compute_predicta(df, PredictaParams())
        expected = df["c"].rolling(5).mean() - df["c"].rolling(35).mean()
        pd.testing.assert_series_equal(result["ewo"], expected, check_names=False)

    def test_buy_label_requires_cross_uptrend_and_positive_delta(self):
        df = frame([100] * 35 + [99, 98, 97, 100, 103, 106, 109, 112])
        result = compute_predicta(df, PredictaParams())
        labels = result.index[result["bull_signal"]].tolist()
        self.assertTrue(labels)
        index = labels[-1]
        self.assertTrue(result.loc[index, "is_uptrend"])
        self.assertGreater(result.loc[index, "delta"], 0)
```

- [ ] **Step 2: Run the tests and verify import failure**

Run: `python -m pytest tests/test_predicta_indicator.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'predicta_indicator'`.

- [ ] **Step 3: Implement the immutable parameters and indicator math**

```python
# predicta_indicator.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PredictaParams:
    ema_fast: int = 8
    ema_slow: int = 21
    supertrend_atr_period: int = 10
    supertrend_factor: float = 3.0
    ewo_fast: int = 5
    ewo_slow: int = 35
    confirm_bars: int = 6
    confirm_atr_buffer: float = 0.08
    stop_atr_mult: float = 0.5


@dataclass(frozen=True)
class PredictaSetupDecision:
    status: Literal["waiting", "confirmed", "invalidated", "timeout"]
    reason: str
    age_bars: int
    confirm_time: int | None = None
    confirm_price: float = 0.0
    stop_price: float = 0.0
    ewo: float = 0.0


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous = frame["c"].shift(1)
    tr = pd.concat([
        frame["h"] - frame["l"],
        (frame["h"] - previous).abs(),
        (frame["l"] - previous).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_predicta(df: pd.DataFrame, params: PredictaParams) -> pd.DataFrame:
    frame = df.copy().reset_index(drop=True)
    for column in ("o", "h", "l", "c", "v"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["ema8"] = frame["c"].ewm(span=params.ema_fast, adjust=False).mean()
    frame["ema21"] = frame["c"].ewm(span=params.ema_slow, adjust=False).mean()
    frame["trend_atr"] = _atr(frame, params.supertrend_atr_period)
    raw_upper = (frame["h"] + frame["l"]) / 2 + params.supertrend_factor * frame["trend_atr"]
    raw_lower = (frame["h"] + frame["l"]) / 2 - params.supertrend_factor * frame["trend_atr"]
    upper = raw_upper.copy()
    lower = raw_lower.copy()
    direction = pd.Series(1, index=frame.index, dtype=int)
    for index in range(1, len(frame)):
        previous_upper = upper.iloc[index - 1]
        previous_lower = lower.iloc[index - 1]
        lower.iloc[index] = max(raw_lower.iloc[index], previous_lower) if frame["c"].iloc[index - 1] > previous_lower else raw_lower.iloc[index]
        upper.iloc[index] = min(raw_upper.iloc[index], previous_upper) if frame["c"].iloc[index - 1] < previous_upper else raw_upper.iloc[index]
        direction.iloc[index] = (
            1 if frame["c"].iloc[index] < lower.iloc[index] else -1
        ) if direction.iloc[index - 1] == -1 else (
            -1 if frame["c"].iloc[index] > upper.iloc[index] else 1
        )
    frame["is_uptrend"] = direction.eq(-1)
    frame["is_downtrend"] = direction.eq(1)
    candle_range = frame["h"] - frame["l"]
    buy = np.where(candle_range > 0, frame["v"] * (frame["c"] - frame["l"]) / candle_range, frame["v"] * 0.5)
    sell = np.where(candle_range > 0, frame["v"] * (frame["h"] - frame["c"]) / candle_range, frame["v"] * 0.5)
    frame["delta"] = buy - sell
    cross_up = frame["ema8"].shift(1).le(frame["ema21"].shift(1)) & frame["ema8"].gt(frame["ema21"])
    cross_down = frame["ema8"].shift(1).ge(frame["ema21"].shift(1)) & frame["ema8"].lt(frame["ema21"])
    frame["bull_signal"] = cross_up & frame["is_uptrend"] & frame["delta"].gt(0)
    frame["bear_signal"] = cross_down & frame["is_downtrend"] & frame["delta"].lt(0)
    frame["ewo"] = frame["c"].rolling(params.ewo_fast).mean() - frame["c"].rolling(params.ewo_slow).mean()
    return frame
```

- [ ] **Step 4: Run formula tests and correct only parity defects**

Run: `python -m pytest tests/test_predicta_indicator.py -v`

Expected: the three formula tests PASS.

- [ ] **Step 5: Add failing six-bar state-machine tests**

```python
from predicta_indicator import evaluate_predicta_setup, make_predicta_setup


def test_breakout_with_wrong_ewo_waits_then_confirms_before_expiry():
    df = frame(np.linspace(90, 110, 45))
    setup = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 37, -1.0, {}, PredictaParams())
    waiting = evaluate_predicta_setup(setup, df.iloc[:43], PredictaParams(), atr_value=1.0)
    confirmed = evaluate_predicta_setup(setup, df.iloc[:44], PredictaParams(), atr_value=1.0)
    assert waiting.status == "waiting"
    assert confirmed.status == "confirmed"


def test_seventh_bar_cannot_confirm():
    df = frame(np.linspace(90, 110, 50))
    setup = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 40, -1.0, {}, PredictaParams())
    decision = evaluate_predicta_setup(setup, df.iloc[:48], PredictaParams(), atr_value=1.0)
    assert decision.status == "timeout"


def test_opposite_close_invalidates_before_confirmation():
    df = frame(np.linspace(90, 110, 45))
    setup = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 40, -1.0, {}, PredictaParams())
    df.loc[41, "c"] = setup["predicta_key_low"] - 0.01
    decision = evaluate_predicta_setup(setup, df.iloc[:42], PredictaParams(), atr_value=1.0)
    assert decision.status == "invalidated"
```

- [ ] **Step 6: Implement deterministic setup construction and evaluation**

Implement these exact signatures in `predicta_indicator.py`:

```python
def make_predicta_setup(
    symbol: str,
    direction: str,
    interval: str,
    frame: pd.DataFrame,
    signal_index: int,
    signal_ewo: float,
    choppy_state: dict,
    params: PredictaParams,
) -> dict:
    row = frame.iloc[signal_index]
    key_time = int(float(row["ot"]))
    key_high = float(row["h"])
    key_low = float(row["l"])
    return {
        "symbol": symbol,
        "direction": direction,
        "source_interval": interval,
        "source_strategy": "predicta_ewo",
        "signal_key": f"PREDICTA|{symbol}|{direction}|{interval}|{key_time}|{key_high:.8f}|{key_low:.8f}",
        "predicta_key_time": key_time,
        "predicta_key_high": key_high,
        "predicta_key_low": key_low,
        "predicta_signal_ewo": float(signal_ewo),
        "predicta_entry_path": "fast" if (
            (direction == "LONG" and float(signal_ewo) > 0)
            or (direction == "SHORT" and float(signal_ewo) < 0)
        ) else "wait",
        "predicta_confirm_bars": params.confirm_bars,
        **dict(choppy_state or {}),
    }


def evaluate_predicta_setup(setup, df, params, atr_value):
    frame = compute_predicta(df, params)
    matches = frame.index[pd.to_numeric(frame["ot"], errors="coerce").eq(int(setup["predicta_key_time"]))].tolist()
    if not matches:
        return PredictaSetupDecision("invalidated", "signal_bar_missing", 0)
    signal_index = matches[-1]
    current_index = len(frame) - 1
    age = current_index - signal_index
    if age <= 0:
        return PredictaSetupDecision("waiting", "await_next_closed_bar", age)
    if age > int(params.confirm_bars):
        return PredictaSetupDecision("timeout", "confirm_window_expired", age)
    current = frame.iloc[current_index]
    direction = str(setup["direction"]).upper()
    close = float(current["c"])
    ewo = float(current["ewo"]) if pd.notna(current["ewo"]) else float("nan")
    key_high = float(setup["predicta_key_high"])
    key_low = float(setup["predicta_key_low"])
    if direction == "LONG" and close < key_low:
        return PredictaSetupDecision("invalidated", "opposite_key_break", age, ewo=ewo)
    if direction == "SHORT" and close > key_high:
        return PredictaSetupDecision("invalidated", "opposite_key_break", age, ewo=ewo)
    confirm_line = key_high + atr_value * params.confirm_atr_buffer if direction == "LONG" else key_low - atr_value * params.confirm_atr_buffer
    price_ok = close > confirm_line if direction == "LONG" else close < confirm_line
    ewo_ok = np.isfinite(ewo) and (ewo > 0 if direction == "LONG" else ewo < 0)
    if not price_ok:
        return PredictaSetupDecision("waiting", "await_price_break", age, ewo=ewo)
    if not ewo_ok:
        return PredictaSetupDecision("waiting", "await_ewo", age, ewo=ewo)
    stop = key_low - atr_value * params.stop_atr_mult if direction == "LONG" else key_high + atr_value * params.stop_atr_mult
    return PredictaSetupDecision(
        "confirmed", "predicta_key_break_ewo", age,
        int(float(current["ot"])), close, stop, ewo,
    )
```

Replace the final comment body with direct code; do not introduce wall-clock time or live prices.

- [ ] **Step 7: Run the complete pure-core test file**

Run: `python -m pytest tests/test_predicta_indicator.py -v`

Expected: PASS, including bar 1, bar 6, bar 7, EWO zero, wrong EWO waiting, and mirrored SHORT cases.

Add assertions that `make_predicta_setup` tags LONG with positive signal EWO and SHORT with negative signal EWO as `fast`, while opposite or zero EWO is tagged `wait`.

- [ ] **Step 8: Commit the pure core**

```powershell
git add predicta_indicator.py tests/test_predicta_indicator.py
git commit -m "feat: add Predicta EWO signal core"
```

---

### Task 2: Configuration and Admin Controls

**Files:**
- Modify: `trader.py:76-227`
- Modify: `trader.py:2548-2559`
- Modify: `admin_server.py:380-553`
- Modify: `demo_bot_config.json`
- Create: `tests/test_predicta_config.py`

**Interfaces:**
- Produces: `TradeConfig` Predicta fields and `_entry_signal_source() == "predicta_ewo"`.
- Consumes: existing `TradeConfig.load/save` and admin `/api/admin/demo/config` proxy behavior.

- [ ] **Step 1: Write failing config and UI tests**

```python
# tests/test_predicta_config.py
import json
import tempfile
import unittest
from pathlib import Path

from trader import TradeConfig, SqueezeBreakoutBot


class PredictaConfigTest(unittest.TestCase):
    def test_config_round_trip_keeps_predicta_fields(self):
        cfg = TradeConfig()
        cfg.entry_signal_source = "predicta_ewo"
        cfg.predicta_confirm_bars = 6
        cfg.predicta_choppy_filter_mode = "hard"
        cfg.predicta_confirm_atr_buffer = 0.08
        cfg.predicta_ewo_fast = 5
        cfg.predicta_ewo_slow = 35
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cfg.json"
            cfg.save(path)
            loaded = TradeConfig.load(path)
        self.assertEqual(loaded.entry_signal_source, "predicta_ewo")
        self.assertEqual(loaded.predicta_choppy_filter_mode, "hard")

    def test_admin_contains_predicta_source_and_controls(self):
        source = Path("admin_server.py").read_text(encoding="utf-8")
        for token in ("predicta_ewo", "dePredictaConfirm", "dePredictaChoppy", "dePredictaEwoFast", "dePredictaEwoSlow"):
            self.assertIn(token, source)
```

- [ ] **Step 2: Run and verify missing-field/source failures**

Run: `python -m pytest tests/test_predicta_config.py -v`

Expected: FAIL because Predicta fields and admin controls are absent.

- [ ] **Step 3: Add exact TradeConfig defaults and source alias**

Add to `TradeConfig`:

```python
predicta_confirm_bars: int = 6
predicta_choppy_filter_mode: str = "hard"
predicta_confirm_atr_buffer: float = 0.08
predicta_ewo_fast: int = 5
predicta_ewo_slow: int = 35
predicta_stop_atr_mult: float = 0.5
predicta_min_stop_pct: float = 0.003
predicta_max_stop_pct: float = 0.08
```

Extend `_entry_signal_source()`:

```python
"predicta": "predicta_ewo",
"predictaewo": "predicta_ewo",
```

and include `predicta_ewo` in the accepted source set.

- [ ] **Step 4: Add the admin option and five Predicta controls**

Add a third option to `deSignalSource`, add numeric/select inputs with the IDs asserted above, and add their exact fields to the save payload. Do not reuse RJ field IDs or hide existing RJ controls.

- [ ] **Step 5: Declare local demo defaults**

Update `demo_bot_config.json` without touching credentials:

```json
"entry_signal_source": "predicta_ewo",
"predicta_confirm_bars": 6,
"predicta_choppy_filter_mode": "hard",
"predicta_confirm_atr_buffer": 0.08,
"predicta_ewo_fast": 5,
"predicta_ewo_slow": 35,
"predicta_stop_atr_mult": 0.5,
"predicta_min_stop_pct": 0.003,
"predicta_max_stop_pct": 0.08
```

- [ ] **Step 6: Run config tests and JSON validation**

Run: `python -m pytest tests/test_predicta_config.py -v`

Run: `python -m json.tool demo_bot_config.json > $null`

Expected: PASS and exit code 0.

- [ ] **Step 7: Commit configuration and controls**

```powershell
git add trader.py admin_server.py demo_bot_config.json tests/test_predicta_config.py
git commit -m "feat: configure Predicta EWO signal source"
```

---

### Task 3: Live Scan and Closed-Bar Candidate Pool

**Files:**
- Modify: `trader.py:35-45`
- Modify: `trader.py:1060-1100`
- Modify: `trader.py:2878-2895`
- Modify: `trader.py:3359-3721`
- Modify: `trader.py:4083-4264`
- Create: `tests/test_predicta_pipeline.py`

**Interfaces:**
- Consumes: Task 1 pure functions and `evaluate_choppy_market_adaptive` from `strategy_filters.py`.
- Produces: `_predicta_setup_from_df`, `_predicta_signal_from_df`, `_scan_predicta_setups`, `_run_predicta_fast_signals`, `_sync_predicta_setup_pool`, `_check_predicta_setup_pool`, and `_run_predicta_cycle`.

- [ ] **Step 1: Write failing hard-choppy and setup creation tests**

```python
# tests/test_predicta_pipeline.py
import logging
import unittest
from unittest.mock import patch

from trader import SqueezeBreakoutBot, TradeConfig


class PredictaPipelineTest(unittest.TestCase):
    def make_bot(self):
        bot = object.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig()
        bot.cfg.entry_signal_source = "predicta_ewo"
        bot.cfg.predicta_choppy_filter_mode = "hard"
        bot._log = logging.getLogger("predicta-test")
        bot._predicta_setup_pool = {}
        bot.positions = []
        bot._used_signal_keys = {}
        bot._failed_signal_keys = {}
        return bot

    @staticmethod
    def bullish_signal_frame():
        close = np.linspace(100.0, 102.0, 40)
        return pd.DataFrame({
            "ot": np.arange(40, dtype=np.int64) * 1_800_000,
            "o": close - 0.1, "h": close + 0.4,
            "l": close - 0.4, "c": close,
            "v": np.full(40, 1000.0),
        })

    @staticmethod
    def bullish_lines(df):
        result = df.copy()
        result["bull_signal"] = False
        result["bear_signal"] = False
        result.loc[result.index[-1], "bull_signal"] = True
        result["is_uptrend"] = True
        result["is_downtrend"] = False
        result["delta"] = 1.0
        result["ewo"] = 1.0
        return result

    @patch("trader.compute_predicta")
    @patch("trader.evaluate_choppy_market_adaptive")
    def test_hard_choppy_signal_never_enters_pool(self, choppy, compute):
        choppy.return_value = {"choppy_filter_is_choppy": True, "choppy_filter_reason": "flat_box"}
        df = self.bullish_signal_frame()
        compute.return_value = self.bullish_lines(df)
        bot = self.make_bot()
        setup = bot._predicta_setup_from_df("BTCUSDT", "30m", df)
        self.assertIsNone(setup)

    @patch("trader.compute_predicta")
    @patch("trader.evaluate_choppy_market_adaptive")
    def test_non_choppy_buy_label_creates_predicta_setup(self, choppy, compute):
        choppy.return_value = {"choppy_filter_is_choppy": False, "choppy_filter_reason": "directional"}
        df = self.bullish_signal_frame()
        compute.return_value = self.bullish_lines(df)
        bot = self.make_bot()
        setup = bot._predicta_setup_from_df("BTCUSDT", "30m", df)
        self.assertEqual(setup["source_strategy"], "predicta_ewo")
        self.assertTrue(setup["signal_key"].startswith("PREDICTA|BTCUSDT|LONG|30m|"))
```

Add `import numpy as np` and `import pandas as pd` to the test file. These tests patch only the already-tested indicator result so they isolate bot routing rather than retesting Task 1 math.

- [ ] **Step 2: Run and verify missing-method failures**

Run: `python -m pytest tests/test_predicta_pipeline.py -v`

Expected: FAIL with missing `_predicta_setup_from_df`.

- [ ] **Step 3: Generalize the choppy adapter without changing RJ behavior**

Extract the body of `_rj_choppy_filter_state` into:

```python
def _choppy_filter_state(self, df, anchor_idx: int, mode: str) -> dict:
    # call evaluate_choppy_market_adaptive with the existing thresholds
    # and return the existing normalized choppy_* evidence keys
```

Keep `_rj_choppy_filter_state` as a wrapper passing `self.cfg.rj_choppy_filter_mode`. Predicta passes `self.cfg.predicta_choppy_filter_mode`. Run `tests/test_choppy_filter.py` immediately after this extraction.

- [ ] **Step 4: Implement signal-K creation from the latest closed row**

`_predicta_setup_from_df(symbol, interval, df)` must:

1. Require at least 35 valid rows.
2. Call `compute_predicta` with config EWO lengths.
3. Inspect only the latest row for `bull_signal` or `bear_signal`.
4. Run hard choppy filtering anchored to that row.
5. Emit `predicta_choppy_reject` on hard rejection.
6. Calculate ATR from data available at the signal close. For a fast setup, set `predicta_stop_price` from the signal K low/high plus 0.5 ATR.
7. Return `make_predicta_setup` on acceptance, tagged `predicta_entry_path=fast` for same-sign signal-bar EWO and `wait` otherwise.

Add `_predicta_signal_from_df(symbol, interval, df)` as the executable-signal adapter. It first checks whether the latest row is a non-choppy Predicta label with same-sign EWO and returns a `predicta_fast_ewo` decision using signal-bar ATR. If not, it scans label indices in the previous six closed bars, applies the same signal-anchored hard choppy filter, calls `evaluate_predicta_setup` for each waiting candidate, rejects candidates with any intervening opposite-key close, and returns only a `confirmed` result whose confirmation bar is the latest row. This is the live/replay parity function used by Task 5; it must not create or mutate the live pool.

- [ ] **Step 5: Write failing pool boundary tests**

Add tests that patch `fetch_klines` with `closed_only=True` and assert:

```python
self.assertEqual(bot._predicta_setup_pool, {})              # after opposite invalidation
self.assertEqual(events[-1][0], "predicta_invalidated")
self.assertIn(key, bot._predicta_setup_pool)                # wrong EWO within bar six
self.assertEqual(entries[0]["source_strategy"], "predicta_ewo")  # valid confirmation
```

Also assert that the seventh bar emits `predicta_timeout` and never calls `enter_predicta_position`.

Add a fast-path test whose signal K has aligned EWO. Assert `enter_predicta_position` is called immediately, `predicta_fast_confirm` is emitted, and `_predicta_setup_pool` remains empty. Add the inverse test proving zero/opposite EWO enters the pool instead.

- [ ] **Step 6: Implement the independent pool**

Initialize:

```python
self._predicta_setup_pool: dict = {}
self._last_predicta_setup_check_ts: float = 0.0
```

Implement `_sync_predicta_setup_pool` and `_check_predicta_setup_pool` using Predicta field names only. Fetch enough closed candles to contain the signal K plus six bars, call `evaluate_predicta_setup`, and map decisions to the specified events. Do not call ticker price or `_seconds_to_interval_close`.

- [ ] **Step 7: Implement scan-cycle routing**

`_scan_predicta_setups` should use `fetch_pairs`, existing tradable-symbol filtering, `MIN_VOLUME` as the universe liquidity floor, the top 500 eligible USDT contracts, four workers, and at least 80 closed bars per symbol. It returns separate `fast_signals` and `waiting_setups`. It must not call RJ watchlist, RJ history stats, RJ divergence, RJ support/resistance, or RJ volume-surge filters.

`_run_predicta_fast_signals(fast_signals, btc_fields)` executes accepted fast signals through `enter_predicta_position`. A signal rejected by BTC, risk, capacity, dedupe, tradability, or order failure emits `predicta_fast_reject` and is never passed to `_sync_predicta_setup_pool`.

`_run_predicta_cycle` calls the fast helper first, then syncs waiting setups into the pool. It updates `last_scan_time`, `last_signal_count`, `last_signals_data`, and emits `predicta_scan_cycle`.

- [ ] **Step 8: Run live-pipeline and RJ regression tests**

Run: `python -m pytest tests/test_predicta_pipeline.py tests/test_choppy_filter.py tests/test_rj_pipeline_view.py -v`

Expected: PASS.

- [ ] **Step 9: Commit the live candidate pipeline**

```powershell
git add trader.py tests/test_predicta_pipeline.py
git commit -m "feat: add Predicta closed-bar candidate pool"
```

---

### Task 4: Shared Key-Candle Entry and No-Time-Stop Exit Policy

**Files:**
- Modify: `trader.py:5175-5493`
- Modify: `trader.py:6108-6225`
- Modify: `trader.py:7059-7420`
- Modify: `tests/test_predicta_pipeline.py`
- Modify: `tests/test_rj_time_stop.py`

**Interfaces:**
- Consumes: confirmed Predicta setup dicts from Task 3.
- Produces: `enter_predicta_position(signal)` and `_is_time_stop_exempt_position(pos)`.

- [ ] **Step 1: Write failing entry-source and time-stop tests**

```python
def test_predicta_entry_wrapper_preserves_source(monkeypatch):
    bot = object.__new__(SqueezeBreakoutBot)
    signal = {
        "symbol": "BTCUSDT", "direction": "LONG", "price": 100.0,
        "predicta_stop_price": 98.0, "source_interval": "30m",
        "source_strategy": "predicta_ewo",
        "signal_key": "PREDICTA|BTCUSDT|LONG|30m|1|101|99",
        "choppy_filter_is_choppy": False,
    }
    captured = {}
    def fake_enter(payload, source_strategy):
        captured.update({"payload": dict(payload), "source_strategy": source_strategy})
        return "position"
    monkeypatch.setattr(bot, "_enter_key_candle_position", fake_enter)
    assert bot.enter_predicta_position(signal) == "position"
    assert captured["source_strategy"] == "predicta_ewo"
    assert captured["payload"]["signal_key"].startswith("PREDICTA|")


def test_fast_rejection_does_not_fall_back_to_pool(monkeypatch):
    bot = self.make_bot()
    bot._predicta_setup_pool = {}
    fast = {
        "symbol": "BTCUSDT", "direction": "LONG", "source_interval": "30m",
        "source_strategy": "predicta_ewo", "predicta_entry_path": "fast",
        "predicta_signal_ewo": 1.0,
        "signal_key": "PREDICTA|BTCUSDT|LONG|30m|1|101|99",
    }
    monkeypatch.setattr(bot, "enter_predicta_position", lambda signal: None)
    bot._run_predicta_fast_signals([fast], {})
    assert bot._predicta_setup_pool == {}


def test_predicta_position_skips_time_stop_even_when_globally_enabled():
    bot = self.make_bot()
    pos = make_position()
    pos.source_strategy = "predicta_ewo"
    pos.signal_key = "PREDICTA|BTCUSDT|LONG|30m|1|101|99"
    reason = bot._time_stop_exit_decision(pos, "30m", 100, -0.9, 0.6, "test")
    assert reason is None
```

Keep existing tests proving structure and RJ time-stop behavior unchanged.

- [ ] **Step 2: Run focused tests and verify failures**

Run: `python -m pytest tests/test_predicta_pipeline.py tests/test_rj_time_stop.py -v`

Expected: Predicta tests FAIL; existing RJ/structure tests PASS.

- [ ] **Step 3: Extract a source-aware shared key-candle entry helper**

Refactor the common portion of `enter_rj_position` into:

```python
def _enter_key_candle_position(self, signal: dict, source_strategy: str) -> Optional[Position]:
    # shared risk, tradable-symbol, dedupe, BTC gate, qty, order, stop,
    # position persistence, entry event, and notification flow
```

Keep wrappers:

```python
def enter_rj_position(self, signal):
    signal["source_strategy"] = "rj_only"
    return self._enter_key_candle_position(signal, "rj_only")

def enter_predicta_position(self, signal):
    signal["source_strategy"] = "predicta_ewo"
    return self._enter_key_candle_position(signal, "predicta_ewo")
```

RJ-only history score gates remain inside the RJ wrapper or conditional RJ branch. Predicta must not read `rj_only_stats_pass`, `rj_volume_filter_pass`, RJ support/resistance, or RJ divergence fields.

- [ ] **Step 4: Add the explicit time-stop exemption**

```python
def _is_time_stop_exempt_position(self, pos: Position) -> bool:
    strategy = str(getattr(pos, "source_strategy", "") or "").strip().lower()
    signal_key = str(getattr(pos, "signal_key", "") or "").upper()
    return strategy == "predicta_ewo" or signal_key.startswith("PREDICTA|")
```

Return `None` at the start of `_time_stop_exit_decision` when this is true. Do not turn off global `enable_time_stop`, because structure and RJ positions must retain their current behavior.

- [ ] **Step 5: Route the live loop**

In `run_once`, branch `predicta_ewo` to `_run_predicta_cycle` and return after updating Predicta status. In `run_loop`, call `_check_predicta_setup_pool` only for `predicta_ewo`; keep `_check_rj_setup_pool` only for `rj_only`.

- [ ] **Step 6: Run entry, exit, persistence, and source regressions**

Run: `python -m pytest tests/test_predicta_pipeline.py tests/test_rj_time_stop.py tests/test_exchange_entry_price_sync.py tests/test_choppy_filter.py -v`

Expected: PASS.

- [ ] **Step 7: Commit shared entry and exit policy**

```powershell
git add trader.py tests/test_predicta_pipeline.py tests/test_rj_time_stop.py
git commit -m "feat: execute Predicta setups with shared risk exits"
```

---

### Task 5: Replay Adapter and Frozen Experiment

**Files:**
- Modify: `strategy_core.py:91-153`
- Modify: `backtest/engine.py:1-260`
- Modify: `backtest/cli.py:80-111`
- Modify: `tests/test_strategy_core.py`
- Modify: `tests/test_backtest_system.py`
- Create: `backtest_data/predicta_ewo_frozen_rules.json`

**Interfaces:**
- Produces: `evaluate_entry(bot, snapshot)` routing to RJ or Predicta while retaining `EntryDecision`.
- Consumes: live `_predicta_signal_from_df`/pure setup evaluation and current `StrategySnapshot` lookahead guard.

- [ ] **Step 1: Write failing live/replay parity tests**

First extend the existing `FakeBot` in `tests/test_strategy_core.py`:

```python
class FakeBot:
    def __init__(self, signal=None, source="rj_only"):
        self.signal = signal
        self.source = source
        self.cfg = type("Cfg", (), {"rj_only_stats_enabled": False, "min_score": 0})()

    def _entry_signal_source(self):
        return self.source

    def _rj_only_signal_from_df(self, symbol, interval, frame):
        return self.signal if self.source == "rj_only" else None

    def _predicta_signal_from_df(self, symbol, interval, frame):
        return self.signal if self.source == "predicta_ewo" else None
```

```python
def test_predicta_entry_adapter_reuses_live_decision(self):
    frame = candles()
    bot = FakeBot(source="predicta_ewo", signal={
        "symbol": "BTCUSDT", "direction": "LONG", "price": 101.0,
        "predicta_stop_price": 99.0,
        "signal_key": "PREDICTA|BTCUSDT|LONG|30m|1|101|99",
        "predicta_key_time": int(frame["ot"].iloc[-2]),
        "predicta_confirm_time": int(frame["ot"].iloc[-1]),
    })
    closed_at = int(frame["ot"].iloc[-1]) + 1_800_000
    decision = evaluate_entry(bot, StrategySnapshot("BTCUSDT", "30m", closed_at, frame, {}))
    self.assertTrue(decision.allowed)
    self.assertEqual(decision.direction, "LONG")
    self.assertEqual(decision.trigger_source, "predicta_key_break_ewo")


def test_predicta_fast_entry_uses_signal_close_decision_time(self):
    frame = candles()
    bot = FakeBot(source="predicta_ewo", signal={
        "symbol": "BTCUSDT", "direction": "LONG", "price": 101.0,
        "predicta_stop_price": 99.0,
        "signal_key": "PREDICTA|BTCUSDT|LONG|30m|1|101|99",
        "predicta_key_time": int(frame["ot"].iloc[-1]),
        "predicta_confirm_time": int(frame["ot"].iloc[-1]),
        "predicta_entry_path": "fast",
        "trigger_source": "predicta_fast_ewo",
    })
    closed_at = int(frame["ot"].iloc[-1]) + 1_800_000
    decision = evaluate_entry(bot, StrategySnapshot("BTCUSDT", "30m", closed_at, frame, {}))
    self.assertTrue(decision.allowed)
    self.assertEqual(decision.trigger_source, "predicta_fast_ewo")


def test_predicta_entry_rejects_unclosed_candle(self):
    frame = candles()
    too_early = int(frame["ot"].iloc[-1]) + 1_799_999
    with self.assertRaisesRegex(ValueError, "strategy_snapshot_lookahead"):
        evaluate_entry(bot, StrategySnapshot("BTCUSDT", "30m", too_early, frame, {}))
```

- [ ] **Step 2: Run and verify missing-router failure**

Run: `python -m pytest tests/test_strategy_core.py -v`

Expected: FAIL because `evaluate_entry` is absent.

- [ ] **Step 3: Add the source-neutral entry router**

```python
def evaluate_entry(bot: Any, snapshot: StrategySnapshot) -> EntryDecision:
    source = bot._entry_signal_source() if hasattr(bot, "_entry_signal_source") else "rj_only"
    if source == "predicta_ewo":
        return evaluate_predicta_entry(bot, snapshot)
    return evaluate_rj_entry(bot, snapshot)
```

`evaluate_predicta_entry` must enforce the same closed-candle guard, call the same Predicta decision path used live, apply the existing BTC gate, and map `predicta_stop_price`, key/confirm times, signal key, generic `trigger_source`, and evidence into `EntryDecision`. Use `predicta_fast_ewo` for the fast path and `predicta_key_break_ewo` for the waiting path.

- [ ] **Step 4: Route both replay engines through `evaluate_entry`**

Replace direct `evaluate_rj_entry` calls. Keep the RJ eligible-count optimization only when source is `rj_only`; for Predicta either add an equivalent bull/bear label prefilter using `compute_predicta` or skip prefiltering for correctness in V1.

Update `_precompute_worker` in `backtest/cli.py` to read `cfg.entry_signal_source`. Preserve the existing RJ trigger prefilter only for `rj_only`; for `predicta_ewo`, evaluate every closed 30m window from `warmup_bars` onward through `evaluate_entry`. Do not call `_compute_rj_lines` in the Predicta branch.

- [ ] **Step 5: Add failing backtest execution tests**

Extend `tests/test_backtest_system.py` so a precomputed Predicta decision:

- fills at the next available 1m open,
- uses a `PREDICTA|`-prefixed dedupe key,
- cannot exceed `max_positions`,
- records `trigger_source=predicta_key_break_ewo`.

Add a separate fast-path replay case proving the fill is the first 1m open at or after the signal-bar decision time and is not the 30m signal close.

- [ ] **Step 6: Create the frozen experiment declaration**

```json
{
  "name": "Predicta-EWO-closed6-choppy-hard-v1",
  "rules": {
    "entry_signal_source": "predicta_ewo",
    "timeframe": "30m",
    "initial_equity": 5000.0,
    "risk_per_trade": 10.0,
    "fee_rate": 0.0006,
    "slippage_bps": 2.0,
    "warmup_bars": 80,
    "max_positions": 6,
    "predicta_confirm_bars": 6,
    "predicta_choppy_filter_mode": "hard",
    "predicta_confirm_atr_buffer": 0.08,
    "predicta_ewo_fast": 5,
    "predicta_ewo_slow": 35,
    "predicta_stop_atr_mult": 0.5,
    "predicta_min_stop_pct": 0.003,
    "predicta_max_stop_pct": 0.08,
    "enable_time_stop": false
  },
  "data_version": "bitget-50d-20260712",
  "train": [1779451200000, 1779883200000],
  "validation": [1779883200000, 1780315200000],
  "test": [1780315200000, 1783769400000],
  "primary_metric": "mean_r",
  "minimum_core_trades": 200,
  "minimum_filter_samples": 50
}
```

The three stop fields are defined in Task 2 and must be loaded with these exact names rather than mapped to RJ fields.

- [ ] **Step 7: Run replay tests**

Run: `python -m pytest tests/test_strategy_core.py tests/test_backtest_system.py -v`

Expected: PASS.

- [ ] **Step 8: Commit replay integration**

```powershell
git add strategy_core.py backtest/engine.py backtest/cli.py tests/test_strategy_core.py tests/test_backtest_system.py backtest_data/predicta_ewo_frozen_rules.json trader.py
git commit -m "feat: replay Predicta EWO decisions"
```

---

### Task 6: Predicta Status, Events, and Documentation

**Files:**
- Modify: `trader.py:7529-7735`
- Modify: `trader.py:7840-8115`
- Modify: `CLAUDE.md:70-125`
- Modify: `tests/test_predicta_pipeline.py`

**Interfaces:**
- Produces: `_predicta_setup_pool_rows()` and `_predicta_pipeline_status()` in fast/full summaries.
- Consumes: Predicta event and pool fields from Tasks 3-4.

- [ ] **Step 1: Write failing status tests**

```python
def test_predicta_status_exposes_pool_and_event_counts():
    bot = self.make_bot()
    bot._predicta_setup_pool = {"PREDICTA|BTC": {
        "symbol": "BTCUSDT", "direction": "LONG", "source_interval": "30m",
        "source_strategy": "predicta_ewo", "signal_key": "PREDICTA|BTC",
        "predicta_key_time": 1_800_000, "predicta_key_high": 101.0,
        "predicta_key_low": 99.0, "predicta_confirm_bars": 6,
    }}
    rows = bot._predicta_setup_pool_rows(limit=40, now_ts=2_000_000.0)
    status = bot._predicta_pipeline_status(rows)
    assert rows[0]["source_strategy"] == "predicta_ewo"
    assert status["setup_pool_size"] == 1
    assert "counts" in status
    assert "fast_confirm_count" in status
```

- [ ] **Step 2: Run and verify missing status methods**

Run: `python -m pytest tests/test_predicta_pipeline.py -v`

Expected: FAIL with missing status methods.

- [ ] **Step 3: Implement source-specific status output**

Expose only public diagnostic fields: symbol, direction, interval, signal time, key high/low, entry path, signal EWO, confirmation line, age bars, expiry, EWO state, choppy reason, and stage. Add `fast_confirm_count`, `fast_reject_count`, `predicta_setup_pool_size`, `predicta_setup_pool`, and `predicta_pipeline` to fast and full summaries without renaming RJ keys.

- [ ] **Step 4: Document the new engine**

Add a concise `Predicta + EWO` section to `CLAUDE.md` containing the exact chain, hard choppy filter, closed-bar rule, six-bar expiry, initial stop, and no-time-stop exception. Do not claim a win rate.

- [ ] **Step 5: Run status and documentation checks**

Run: `python -m pytest tests/test_predicta_pipeline.py tests/test_rj_pipeline_view.py -v`

Run: `Select-String -Path CLAUDE.md -Pattern 'predicta_ewo','不执行时间止损','6根'`

Expected: tests PASS and all three documentation patterns are present.

- [ ] **Step 6: Commit observability and documentation**

```powershell
git add trader.py CLAUDE.md tests/test_predicta_pipeline.py
git commit -m "feat: expose Predicta strategy diagnostics"
```

---

### Task 7: Full Verification and Evidence Report

**Files:**
- Verify: all modified files
- Do not modify: `PROGRESS.md` unless a later server deployment is explicitly authorized and completed

**Interfaces:**
- Consumes: completed implementation from Tasks 1-6.
- Produces: verified local implementation and an evidence-backed handoff.

- [ ] **Step 1: Compile every changed Python entry point**

Run:

```powershell
python -m py_compile predicta_indicator.py trader.py strategy_core.py backtest\engine.py admin_server.py web_ui.py
```

Expected: exit code 0 with no output.

- [ ] **Step 2: Run targeted strategy suites**

Run:

```powershell
python -m pytest tests/test_predicta_indicator.py tests/test_predicta_config.py tests/test_predicta_pipeline.py tests/test_strategy_core.py tests/test_backtest_system.py tests/test_choppy_filter.py tests/test_rj_time_stop.py tests/test_rj_pipeline_view.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full regression suite**

Run: `python -m pytest tests -v`

Expected: PASS with zero failures and zero errors.

- [ ] **Step 4: Validate config and diff hygiene**

Run:

```powershell
python -m json.tool demo_bot_config.json > $null
python -m json.tool backtest_data\predicta_ewo_frozen_rules.json > $null
git diff --check
git status --short
```

Expected: JSON commands and `git diff --check` exit 0; status contains only intentional implementation files.

- [ ] **Step 5: Run the frozen local replay if required 1m/30m files are present**

Run:

```powershell
python replay_engine.py portfolio --root backtest_data --symbols backtest_data\universe_liquid_192_50d_20260712.txt --experiment backtest_data\predicta_ewo_frozen_rules.json --output backtest_runs --workers 4
```

Expected: a new immutable run directory with `manifest.json`, `events.jsonl`, `metrics.json`, and `reconciliation.json`. If input files are missing, report the exact missing intervals; do not synthesize candles or substitute another data version.

- [ ] **Step 6: Inspect required evidence without beautifying it**

Report verbatim:

- raw Predicta label count,
- same-sign EWO fast-decision, fast-fill, and fast-reject counts,
- `predicta_choppy_reject` count and rate,
- setup count,
- `predicta_wait_ewo` count,
- confirmed/filled count,
- trades, win rate, mean/median/sum R, Profit Factor, max drawdown R,
- fees, slippage, MFE, MAE, final equity, and sample status.

If trades are below 200, retain `SAMPLE_NOT_READY` and do not call the strategy profitable.

- [ ] **Step 7: Commit any verification-only fixes, then re-run affected checks**

```powershell
git add predicta_indicator.py trader.py strategy_core.py backtest\engine.py admin_server.py web_ui.py tests
git commit -m "fix: close Predicta verification gaps"
```

Skip this commit when verification requires no fixes.

- [ ] **Step 8: Hand off without deploying**

State what passed, what replay evidence showed, and what could not be verified. Explicitly say that server deployment and `PROGRESS.md` deployment bookkeeping remain pending separate authorization.
