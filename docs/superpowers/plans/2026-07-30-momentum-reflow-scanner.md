# Momentum Reflow Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only 1-hour momentum reflow scanner that tracks strong EMA50 breakouts and displays only the first continuous 1-to-5-candle return window with a confirmed daily filter.

**Architecture:** Put all indicator, daily-pattern, state-machine, persistence, futures-universe, and scan orchestration logic in a new focused `momentum_reflow.py` module. Keep `web_ui.py` responsible only for the existing asynchronous scan controller, route selection, menu metadata, and rendering. Persist per-symbol cursors and event state in an atomically written JSON ledger so observed events have no fixed expiry and survive restarts.

**Tech Stack:** Python 3.12, pandas, NumPy, requests, Flask, native HTML/CSS/JavaScript, `unittest`, Node.js for rendering tests, Playwright CLI for final browser verification.

## Global Constraints

- Use Binance mainnet USDT-margined futures data, not spot data and not testnet data.
- Analyze only fully closed `1h` and `1d` candles.
- Use EMA50 as the only moving-average reference; do not add EMA20, EMA100, EMA200, or a multi-MA alignment filter.
- Breakout candle body must be at least `0.8 × ATR(14)` and volume at least `1.5 ×` the prior 20 closed candles' average.
- Breakout expansion must reach `1.5 × ATR(14)` on the breakout candle or within the next 3 closed 1-hour candles.
- EMA50 slope is directional when current EMA50 is above/below EMA50 from 3 candles earlier.
- Touch zone is `EMA50 ± 0.2 ATR`; wick or body overlap counts.
- A displayed candle must satisfy `abs(close - EMA50) / ATR ≤ 0.35`; close may be on either side of EMA50.
- Display at most 5 consecutive closed 1-hour candles from the first qualifying touch; leaving the zone or exceeding the close-distance limit consumes the event.
- Do not add a fixed time expiry to an observed expanded event.
- Daily confirmation is a directional strong daily candle, one of the approved reversal patterns, or a confirmed directional 3-candle fractal.
- Keep the scanner read-only and isolated from Predicta, RJ, automatic entries, risk, positions, stops, exits, PnL, and account configuration.
- Do not add external frontend frameworks or npm dependencies.
- Any server deployment requires a fresh backup and explicit user approval immediately before deployment.
- After a verified server deployment, update local `PROGRESS.md` with the change, deployed files, backup action, and verification result.

---

### Task 1: Pure indicators and daily confirmation

**Files:**
- Create: `momentum_reflow.py`
- Create: `tests/test_momentum_reflow.py`

**Interfaces:**
- Produces: `add_hourly_indicators(frame: pd.DataFrame) -> pd.DataFrame`
- Produces: `add_daily_indicators(frame: pd.DataFrame) -> pd.DataFrame`
- Produces: `daily_confirmation(frame: pd.DataFrame, direction: str) -> dict`
- The returned daily dictionary has exactly `passed: bool`, `kind: str`, and `rank: int`.
- Required input columns are `ot`, `o`, `h`, `l`, `c`, and `v`.

- [ ] **Step 1: Write failing hourly indicator tests**

Add deterministic fixtures and boundary assertions:

```python
import unittest

import numpy as np
import pandas as pd

from momentum_reflow import add_hourly_indicators


def candle_frame(count=80, start=100.0):
    close = np.linspace(start, start + count - 1, count)
    return pd.DataFrame({
        "ot": np.arange(count, dtype=np.int64) * 3_600_000,
        "o": close - 0.4,
        "h": close + 1.0,
        "l": close - 1.0,
        "c": close,
        "v": np.full(count, 100.0),
    })


class HourlyIndicatorTests(unittest.TestCase):
    def test_adds_ema50_atr14_and_prior_volume_average(self):
        out = add_hourly_indicators(candle_frame())
        self.assertEqual(
            {"ema50", "atr14", "vol_ma20_prev"}.issubset(out.columns),
            True,
        )
        self.assertTrue(np.isfinite(out.iloc[-1]["ema50"]))
        self.assertTrue(np.isfinite(out.iloc[-1]["atr14"]))
        self.assertEqual(out.iloc[-1]["vol_ma20_prev"], 100.0)

    def test_volume_average_excludes_current_breakout_candle(self):
        frame = candle_frame()
        frame.loc[frame.index[-1], "v"] = 1_000.0
        out = add_hourly_indicators(frame)
        self.assertEqual(out.iloc[-1]["vol_ma20_prev"], 100.0)
```

- [ ] **Step 2: Run the hourly tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow.py" -v
```

Expected: import failure because `momentum_reflow.py` does not exist.

- [ ] **Step 3: Implement the indicator functions**

Create `momentum_reflow.py` with these constants and formulas:

```python
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

EMA_PERIOD = 50
ATR_PERIOD = 14
VOLUME_PERIOD = 20
BREAKOUT_BODY_ATR = 0.8
BREAKOUT_VOLUME_RATIO = 1.5
EXPANSION_ATR = 1.5
EXPANSION_FOLLOW_BARS = 3
EMA_SLOPE_BARS = 3
TOUCH_ZONE_ATR = 0.2
CLOSE_DISTANCE_ATR = 0.35
RETURN_WINDOW_BARS = 5
LEDGER_VERSION = 1


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["c"].shift(1)
    return pd.concat(
        [
            frame["h"] - frame["l"],
            (frame["h"] - previous_close).abs(),
            (frame["l"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def add_hourly_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values("ot").drop_duplicates("ot").reset_index(drop=True).copy()
    out["ema50"] = out["c"].ewm(span=EMA_PERIOD, adjust=False).mean()
    out["atr14"] = _true_range(out).rolling(ATR_PERIOD).mean()
    out["vol_ma20_prev"] = out["v"].shift(1).rolling(VOLUME_PERIOD).mean()
    return out


def add_daily_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values("ot").drop_duplicates("ot").reset_index(drop=True).copy()
    out["atr14"] = _true_range(out).rolling(ATR_PERIOD).mean()
    out["vol_ma20_prev"] = out["v"].shift(1).rolling(VOLUME_PERIOD).mean()
    return out
```

- [ ] **Step 4: Write failing daily confirmation tests**

Cover strong candles, all six approved patterns, fractals, direction mismatch, and insufficient history:

```python
from momentum_reflow import daily_confirmation


class DailyConfirmationTests(unittest.TestCase):
    def test_directional_strong_daily_candle(self):
        frame = candle_frame(40)
        frame.loc[frame.index[-1], ["o", "h", "l", "c", "v"]] = [
            100.0, 112.0, 99.0, 111.0, 300.0
        ]
        result = daily_confirmation(frame, "LONG")
        self.assertEqual(result, {"passed": True, "kind": "strong_momentum", "rank": 3})
        self.assertFalse(daily_confirmation(frame, "SHORT")["passed"])

    def test_confirmed_bottom_and_top_fractals(self):
        bottom = candle_frame(40)
        bottom.loc[37, "l"], bottom.loc[38, "l"], bottom.loc[39, "l"] = 90.0, 80.0, 91.0
        self.assertEqual(
            daily_confirmation(bottom, "LONG")["kind"],
            "bottom_fractal",
        )
        top = candle_frame(40)
        top.loc[37, "h"], top.loc[38, "h"], top.loc[39, "h"] = 110.0, 120.0, 109.0
        self.assertEqual(
            daily_confirmation(top, "SHORT")["kind"],
            "top_fractal",
        )

    def test_reversal_pattern_matrix(self):
        expected = {
            "LONG": {"bullish_engulfing", "hammer", "morning_star"},
            "SHORT": {"bearish_engulfing", "shooting_star", "evening_star"},
        }
        for direction, kinds in expected.items():
            observed = {
                daily_confirmation(make_daily_pattern(kind), direction)["kind"]
                for kind in kinds
            }
            self.assertEqual(observed, kinds)
```

Define `make_daily_pattern(kind)` in the test file from a flat 40-row OHLCV base (`o=100`, `h=101`, `l=99`, `c=100`, `v=100`) and replace the final rows with these exact values:

```python
PATTERN_ROWS = {
    "bullish_engulfing": [
        (101.0, 102.0, 98.0, 99.0),
        (98.5, 102.0, 98.0, 101.5),
    ],
    "bearish_engulfing": [
        (99.0, 102.0, 98.0, 101.0),
        (101.5, 102.0, 98.0, 98.5),
    ],
    "hammer": [
        (100.5, 101.2, 98.5, 101.0),
    ],
    "shooting_star": [
        (100.5, 102.5, 99.8, 100.0),
    ],
    "morning_star": [
        (102.0, 102.5, 97.5, 98.0),
        (99.2, 100.0, 98.8, 99.6),
        (99.5, 101.0, 99.0, 100.5),
    ],
    "evening_star": [
        (98.0, 102.5, 97.5, 102.0),
        (100.4, 101.2, 100.0, 100.8),
        (100.5, 101.0, 99.0, 99.5),
    ],
}


def make_daily_pattern(kind):
    frame = pd.DataFrame({
        "ot": np.arange(40, dtype=np.int64) * 86_400_000,
        "o": np.full(40, 100.0),
        "h": np.full(40, 101.0),
        "l": np.full(40, 99.0),
        "c": np.full(40, 100.0),
        "v": np.full(40, 100.0),
    })
    values = PATTERN_ROWS[kind]
    start = len(frame) - len(values)
    for index, (open_, high, low, close) in enumerate(values, start=start):
        frame.loc[index, ["o", "h", "l", "c"]] = [open_, high, low, close]
    return frame
```

Each pattern fixture keeps volume at the baseline so it cannot pass the strong-momentum volume condition.

- [ ] **Step 5: Implement exact daily rules**

Implement `daily_confirmation` using these deterministic definitions:

```python
def _body(row) -> float:
    return abs(float(row["c"]) - float(row["o"]))


def _range(row) -> float:
    return max(0.0, float(row["h"]) - float(row["l"]))


def _bullish_engulfing(previous, current) -> bool:
    return (
        previous["c"] < previous["o"]
        and current["c"] > current["o"]
        and current["o"] <= previous["c"]
        and current["c"] >= previous["o"]
    )


def _bearish_engulfing(previous, current) -> bool:
    return (
        previous["c"] > previous["o"]
        and current["c"] < current["o"]
        and current["o"] >= previous["c"]
        and current["c"] <= previous["o"]
    )


def _hammer(row) -> bool:
    body = max(_body(row), 1e-12)
    lower = min(row["o"], row["c"]) - row["l"]
    upper = row["h"] - max(row["o"], row["c"])
    return row["c"] > row["o"] and lower >= 2.0 * body and upper <= body


def _shooting_star(row) -> bool:
    body = max(_body(row), 1e-12)
    upper = row["h"] - max(row["o"], row["c"])
    lower = min(row["o"], row["c"]) - row["l"]
    return row["c"] < row["o"] and upper >= 2.0 * body and lower <= body
```

For morning/evening stars, require:

- first candle body at least 60% of its high-low range;
- middle candle body at most 40% of the first candle body;
- third candle closes beyond the midpoint of the first candle body;
- first and third candles have the required opposite directions;
- do not require gaps.

Return the first match in this priority: strong momentum rank 3, three-candle star rank 2, engulfing rank 2, hammer/shooting star rank 2, confirmed fractal rank 1. Return `{"passed": False, "kind": "none", "rank": 0}` when no rule passes.

- [ ] **Step 6: Run Task 1 tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow.py" -v
```

Expected: all Task 1 tests pass.

- [ ] **Step 7: Commit Task 1**

```powershell
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: add momentum reflow indicators"
```

---

### Task 2: Event state machine and atomic ledger

**Files:**
- Modify: `momentum_reflow.py`
- Modify: `tests/test_momentum_reflow.py`

**Interfaces:**
- Consumes: indicator columns from Task 1.
- Produces: `advance_symbol(symbol: str, symbol_state: dict, frame: pd.DataFrame) -> tuple[dict, dict | None]`
- Produces: `load_ledger(path: Path) -> dict`
- Produces: `save_ledger(path: Path, ledger: dict) -> None`
- A symbol state has `last_processed_open_time: int` and `event: dict | None`.
- An event has `direction`, `state`, `breakout_open_time`, `breakout_volume_ratio`, `expansion_time`, `max_expansion_atr`, `first_touch_time`, `return_window_index`, and `audit_reason`.

- [ ] **Step 1: Write the failing state-machine matrix**

Add tests that build indicator-ready rows with fixed EMA and ATR values:

```python
from momentum_reflow import advance_symbol

HOUR_MS = 3_600_000
BASE_OT = 100 * HOUR_MS


def make_waiting_state(direction):
    return {
        "last_processed_open_time": BASE_OT,
        "event": {
            "direction": direction,
            "state": "WAIT_FIRST_RETURN",
            "breakout_open_time": BASE_OT - 4 * HOUR_MS,
            "breakout_volume_ratio": 2.0,
            "expansion_time": BASE_OT - 3 * HOUR_MS,
            "max_expansion_atr": 2.0,
            "first_touch_time": 0,
            "return_window_index": 0,
            "audit_reason": "expansion_confirmed",
        },
    }


def make_touch_frame(
    close=100.1,
    ema50=100.0,
    atr14=1.0,
    offset=1,
    direction="LONG",
):
    target = BASE_OT + offset * HOUR_MS
    ema_values = (
        [ema50 - 0.3, ema50 - 0.2, ema50 - 0.1, ema50]
        if direction == "LONG"
        else [ema50 + 0.3, ema50 + 0.2, ema50 + 0.1, ema50]
    )
    closes = [ema_values[0], ema_values[1], ema_values[2], close]
    rows = []
    for index in range(4):
        row_close = closes[index]
        rows.append({
            "ot": target - (3 - index) * HOUR_MS,
            "o": row_close,
            "h": max(row_close, ema_values[index] + 0.1),
            "l": min(row_close, ema_values[index] - 0.1),
            "c": row_close,
            "v": 100.0,
            "ema50": ema_values[index],
            "atr14": atr14,
            "vol_ma20_prev": 100.0,
        })
    rows[-1]["h"] = max(close, ema50 + 0.2 * atr14)
    rows[-1]["l"] = min(close, ema50 - 0.2 * atr14)
    return pd.DataFrame(rows)


def make_far_frame(offset=2):
    frame = make_touch_frame(close=102.0, offset=offset)
    frame.loc[frame.index[-1], ["h", "l"]] = [102.2, 101.8]
    return frame


def make_state_machine_frame(direction):
    sign = 1.0 if direction == "LONG" else -1.0
    ema_values = [99.7, 99.8, 99.9, 100.0, 100.1, 100.2]
    if direction == "SHORT":
        ema_values = [100.2, 100.1, 100.0, 99.9, 99.8, 99.7]
    below = [value - 0.1 * sign for value in ema_values[:4]]
    breakout_close = ema_values[4] + 1.6 * sign
    touch_close = ema_values[5] + 0.05 * sign
    closes = below + [breakout_close, touch_close]
    rows = []
    for index, (ema_value, close_value) in enumerate(zip(ema_values, closes)):
        open_value = close_value
        volume = 100.0
        if index == 4:
            open_value = ema_value - 1.0 * sign
            volume = 200.0
        high = max(open_value, close_value) + 0.1
        low = min(open_value, close_value) - 0.1
        if index == 5:
            high = max(high, ema_value + 0.2)
            low = min(low, ema_value - 0.2)
        rows.append({
            "ot": (BASE_OT - 5 * HOUR_MS) + index * HOUR_MS,
            "o": open_value,
            "h": high,
            "l": low,
            "c": close_value,
            "v": volume,
            "ema50": ema_value,
            "atr14": 1.0,
            "vol_ma20_prev": 100.0,
        })
    return pd.DataFrame(rows)


class ReflowStateMachineTests(unittest.TestCase):
    def test_long_breakout_expansion_and_first_wick_touch(self):
        frame = make_state_machine_frame("LONG")
        state, candidate = advance_symbol("TESTUSDT", {}, frame)
        self.assertEqual(state["event"]["state"], "RETURN_WINDOW")
        self.assertEqual(state["event"]["return_window_index"], 1)
        self.assertEqual(candidate["direction"], "LONG")
        self.assertEqual(candidate["window_index"], 1)

    def test_close_may_finish_on_either_side_of_ema(self):
        for close in (99.70, 100.30):
            frame = make_touch_frame(close=close, ema50=100.0, atr14=1.0)
            state, candidate = advance_symbol("TESTUSDT", make_waiting_state("LONG"), frame)
            self.assertIsNotNone(candidate)

    def test_first_bad_touch_consumes_event(self):
        frame = make_touch_frame(close=100.36, ema50=100.0, atr14=1.0)
        state, candidate = advance_symbol("TESTUSDT", make_waiting_state("LONG"), frame)
        self.assertIsNone(candidate)
        self.assertEqual(state["event"]["state"], "CONSUMED")

    def test_return_window_is_consecutive_and_capped_at_five(self):
        state = make_waiting_state("SHORT")
        for expected_index in range(1, 6):
            state, candidate = advance_symbol(
                "TESTUSDT",
                state,
                make_touch_frame(
                    close=99.9,
                    ema50=100.0,
                    atr14=1.0,
                    offset=expected_index,
                    direction="SHORT",
                ),
            )
            self.assertEqual(candidate["window_index"], expected_index)
        self.assertEqual(state["event"]["state"], "CONSUMED")

    def test_leaving_zone_consumes_and_never_reopens_same_event(self):
        state, _ = advance_symbol(
            "TESTUSDT", make_waiting_state("LONG"), make_touch_frame(close=100.1)
        )
        state, candidate = advance_symbol("TESTUSDT", state, make_far_frame())
        self.assertIsNone(candidate)
        self.assertEqual(state["event"]["state"], "CONSUMED")
        state, candidate = advance_symbol("TESTUSDT", state, make_touch_frame(close=100.1, offset=3))
        self.assertIsNone(candidate)
```

Also test:

- long and short symmetry;
- expansion on breakout candle;
- expansion on each of the next 3 candles;
- invalidation after the third follow-up candle;
- no elapsed-time invalidation in `WAIT_FIRST_RETURN`;
- EMA slope invalidation before first touch;
- same candle is not processed twice;
- a later new strong breakout replaces a consumed or invalidated event.

- [ ] **Step 2: Run the state-machine tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow.py" -v
```

Expected: import failure for `advance_symbol`.

- [ ] **Step 3: Implement the state transitions**

Implement small pure helpers:

```python
def _touches_zone(row) -> bool:
    lower = row["ema50"] - TOUCH_ZONE_ATR * row["atr14"]
    upper = row["ema50"] + TOUCH_ZONE_ATR * row["atr14"]
    return row["l"] <= upper and row["h"] >= lower


def _close_distance_atr(row) -> float:
    return abs(row["c"] - row["ema50"]) / row["atr14"]


def _slope_aligned(frame: pd.DataFrame, index: int, direction: str) -> bool:
    if index < EMA_SLOPE_BARS:
        return False
    now = frame.iloc[index]["ema50"]
    prior = frame.iloc[index - EMA_SLOPE_BARS]["ema50"]
    return now > prior if direction == "LONG" else now < prior


def _breakout_direction(previous, current) -> str | None:
    body_ratio = abs(current["c"] - current["o"]) / current["atr14"]
    volume_ratio = current["v"] / current["vol_ma20_prev"]
    if body_ratio < BREAKOUT_BODY_ATR or volume_ratio < BREAKOUT_VOLUME_RATIO:
        return None
    if previous["c"] <= previous["ema50"] and current["c"] > current["ema50"]:
        return "LONG"
    if previous["c"] >= previous["ema50"] and current["c"] < current["ema50"]:
        return "SHORT"
    return None
```

`advance_symbol` must:

1. sort and deduplicate rows;
2. process only rows whose `ot` is greater than `last_processed_open_time`;
3. ignore non-finite indicator rows without moving the cursor past them;
4. create `WAIT_EXPANSION` on a qualified breakout;
5. compute directional close-to-EMA expansion and permit confirmation on the breakout row;
6. invalidate after exactly 3 later rows without expansion;
7. in `WAIT_FIRST_RETURN`, invalidate only on EMA slope reversal, not elapsed time and not a close across EMA;
8. consume the first touch when close distance exceeds 0.35 ATR;
9. keep `RETURN_WINDOW` only while both touch and close-distance rules pass;
10. emit a candidate only when the latest processed closed candle is an active return-window candle;
11. set `CONSUMED` after emitting window index 5;
12. allow a later qualified breakout to create a new event after `CONSUMED` or `INVALIDATED`.
13. when a repeated scan contains no new candle, reconstruct and return the current candidate if the saved event is still an active `RETURN_WINDOW`; do not increment its window index twice.

- [ ] **Step 4: Write failing ledger tests**

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from momentum_reflow import load_ledger, save_ledger


class ReflowLedgerTests(unittest.TestCase):
    def test_round_trip_preserves_active_event_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger = {
                "version": 1,
                "symbols": {"TESTUSDT": make_waiting_state("LONG")},
            }
            save_ledger(path, ledger)
            self.assertEqual(load_ledger(path), ledger)

    def test_corrupt_file_is_not_replaced_with_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_ledger(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_failed_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            original = {"version": 1, "symbols": {}}
            save_ledger(path, original)
            with patch("momentum_reflow.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_ledger(path, {"version": 1, "symbols": {"X": {}}})
            self.assertEqual(load_ledger(path), original)
```

- [ ] **Step 5: Implement versioned atomic persistence**

Use the same-directory temporary file, explicit flush, `os.fsync`, and `os.replace`:

```python
import json
import os
import tempfile


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"version": LEDGER_VERSION, "symbols": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != LEDGER_VERSION or not isinstance(data.get("symbols"), dict):
        raise ValueError("momentum reflow ledger version or shape is invalid")
    return data


def save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
```

- [ ] **Step 6: Run Task 2 tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow.py" -v
```

Expected: all Task 2 tests pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: track first EMA50 reflow lifecycle"
```

---

### Task 3: Binance futures universe and incremental scan service

**Files:**
- Modify: `momentum_reflow.py`
- Modify: `tests/test_momentum_reflow.py`

**Interfaces:**
- Consumes: `screener.fetch_klines` and `screener.fetch_klines_range` with `market_type="futures"` and `testnet=False`.
- Produces: `fetch_futures_universe() -> tuple[list[str], dict[str, float]]`
- Produces: `scan_momentum_reflow(ledger_path: Path, progress: Callable[[int, int], None] | None = None, max_workers: int = 12) -> dict`
- Scan payload has `rows: list[dict]`, `scanned: int`, `errors: int`, and `initialized: int`.

- [ ] **Step 1: Write failing futures-universe and routing tests**

```python
from unittest.mock import Mock, patch

from momentum_reflow import fetch_futures_universe, scan_momentum_reflow


class ReflowScanServiceTests(unittest.TestCase):
    @patch("momentum_reflow.requests.get")
    def test_universe_uses_fapi_usdt_perpetual_contracts(self, get):
        exchange = Mock()
        exchange.raise_for_status.return_value = None
        exchange.json.return_value = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                },
                {
                    "symbol": "BTCUSDC",
                    "quoteAsset": "USDC",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                },
            ]
        }
        ticker = Mock()
        ticker.raise_for_status.return_value = None
        ticker.json.return_value = [{"symbol": "BTCUSDT", "quoteVolume": "9000000"}]
        get.side_effect = [exchange, ticker]
        symbols, volume = fetch_futures_universe()
        self.assertEqual(symbols, ["BTCUSDT"])
        self.assertEqual(volume["BTCUSDT"], 9_000_000.0)
        self.assertIn("/fapi/v1/exchangeInfo", get.call_args_list[0].args[0])

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_active_ledger_symbol_is_scanned_below_current_volume_filter(
        self, universe, latest, ranged
    ):
        universe.return_value = (["NEWUSDT"], {"NEWUSDT": 9_000_000.0})
        latest.side_effect = lambda symbol, interval, *args, **kwargs: (
            make_closed_hourly_history(symbol)
            if interval == "1h"
            else make_closed_daily_history(symbol)
        )
        ranged.side_effect = lambda symbol, *args, **kwargs: (
            make_closed_hourly_history(symbol)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {
                "version": 1,
                "symbols": {"OLDUSDT": make_waiting_state("LONG")},
            })
            payload = scan_momentum_reflow(path, max_workers=1)
        requested = {
            call.args[0] for call in latest.call_args_list + ranged.call_args_list
        }
        self.assertEqual(requested, {"NEWUSDT", "OLDUSDT"})
        self.assertEqual(payload["scanned"], 2)

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_gap_failure_preserves_symbol_state_and_cursor(
        self, universe, latest, ranged
    ):
        universe.return_value = ([], {})
        original = make_waiting_state("LONG")
        gapped = make_closed_hourly_history("TESTUSDT")
        gapped = gapped.drop(gapped.index[-2]).reset_index(drop=True)
        ranged.return_value = gapped
        latest.return_value = make_closed_daily_history("TESTUSDT")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {
                "version": 1,
                "symbols": {"TESTUSDT": original},
            })
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(saved["symbols"]["TESTUSDT"], original)
```

Use these raw exchange-frame helpers:

```python
def make_closed_hourly_history(symbol):
    count = 100
    close = np.linspace(90.0, 100.0, count)
    return pd.DataFrame({
        "ot": (BASE_OT + HOUR_MS) - np.arange(count - 1, -1, -1) * HOUR_MS,
        "o": close - 0.05,
        "h": close + 0.10,
        "l": close - 0.10,
        "c": close,
        "v": np.full(count, 100.0),
    })


def make_closed_daily_history(symbol):
    count = 40
    close = np.full(count, 100.0)
    frame = pd.DataFrame({
        "ot": np.arange(count, dtype=np.int64) * 86_400_000,
        "o": close.copy(),
        "h": close + 1.0,
        "l": close - 1.0,
        "c": close.copy(),
        "v": np.full(count, 100.0),
    })
    frame.loc[frame.index[-1], ["o", "h", "l", "c", "v"]] = [
        100.0, 112.0, 99.0, 111.0, 300.0
    ]
    return frame
```

Both helpers return only `ot/o/h/l/c/v`; they do not precompute indicators.

- [ ] **Step 2: Run scan-service tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow.py" -v
```

Expected: import failure for `fetch_futures_universe`.

- [ ] **Step 3: Implement futures universe retrieval**

Use only these production endpoints:

```python
import requests
from collections.abc import Callable

from screener import (
    MIN_PRICE,
    MIN_VOLUME,
    fetch_klines,
    fetch_klines_range,
    is_tradfi_or_junk,
)

FUTURES_BASE = "https://fapi.binance.com"


def fetch_futures_universe() -> tuple[list[str], dict[str, float]]:
    exchange_response = requests.get(
        f"{FUTURES_BASE}/fapi/v1/exchangeInfo", timeout=10
    )
    exchange_response.raise_for_status()
    ticker_response = requests.get(
        f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=10
    )
    ticker_response.raise_for_status()
    volume = {
        row["symbol"]: float(row["quoteVolume"])
        for row in ticker_response.json()
        if row.get("symbol") and row.get("quoteVolume") is not None
    }
    symbols = [
        row["symbol"]
        for row in exchange_response.json().get("symbols", [])
        if row.get("quoteAsset") == "USDT"
        and row.get("contractType") == "PERPETUAL"
        and row.get("status") == "TRADING"
        and not is_tradfi_or_junk(row.get("symbol", ""))
        and volume.get(row.get("symbol", ""), 0.0) >= MIN_VOLUME
    ]
    return symbols, volume
```

- [ ] **Step 4: Implement closed-candle incremental retrieval**

For a new symbol:

```python
hourly = fetch_klines(
    symbol,
    "1h",
    1000,
    exchange="binance",
    closed_only=True,
    market_type="futures",
    testnet=False,
)
```

For an existing symbol, request up to 1000 closed hours before its cursor so EMA50, ATR14, and volume context match initialization:

```python
context_start = max(0, last_processed_open_time - 1000 * 3_600_000)
hourly = fetch_klines_range(
    symbol,
    "1h",
    context_start,
    exchange="binance",
    market_type="futures",
    testnet=False,
)
```

Daily input:

```python
daily = fetch_klines(
    symbol,
    "1d",
    40,
    exchange="binance",
    closed_only=True,
    market_type="futures",
    testnet=False,
)
```

Validate that `ot` is strictly increasing by exactly 3,600,000 milliseconds for every new 1-hour candle between the saved cursor and the latest closed candle. Duplicate, missing, or out-of-order new rows fail that symbol without advancing its state.

- [ ] **Step 5: Implement scan orchestration**

`scan_momentum_reflow` must:

1. load the ledger before fetching market data;
2. build `eligible_symbols ∪ active_ledger_symbols`;
3. process up to 12 symbols concurrently;
4. never mutate shared ledger state inside workers;
5. collect each worker's proposed state and candidate;
6. preserve the old symbol state on any worker error;
7. apply `daily_confirmation` only when producing the current candidate;
8. retain active return-window state when daily confirmation fails, while omitting the row;
9. skip initialization when the latest closed price is below `MIN_PRICE`;
10. atomically save the merged ledger once after all workers finish;
11. sort rows by absolute EMA distance, negative daily rank, and negative breakout volume ratio;
12. return only the first 80 rows to the UI while retaining all states;
13. call `progress(completed, total)` after each completed worker when a callback is provided.

Include these output keys per row:

```python
{
    "symbol": "BTCUSDT",
    "direction": "LONG",
    "price": 100.0,
    "ema50": 99.9,
    "close_distance_atr": 0.1,
    "window_index": 1,
    "breakout_time": 1780000000000,
    "max_expansion_atr": 2.1,
    "daily_kind": "strong_momentum",
    "daily_rank": 3,
    "breakout_volume_ratio": 1.8,
}
```

- [ ] **Step 6: Run all momentum-reflow module tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow.py" -v
```

Expected: all module tests pass.

- [ ] **Step 7: Commit Task 3**

```powershell
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: scan Binance futures momentum reflows"
```

---

### Task 4: Flask route, sidebar entry, and result rendering

**Files:**
- Modify: `web_ui.py`
- Create: `tests/test_momentum_reflow_integration.py`

**Interfaces:**
- Consumes: `scan_momentum_reflow(Path, progress, max_workers) -> dict`.
- Adds cache key `reflow_1h`.
- Adds existing generic route behavior for `/scan/reflow/1h`.
- Adds frontend tab id `reflow_1h`.

- [ ] **Step 1: Write failing source and route integration tests**

```python
import importlib
import unittest
from unittest.mock import patch


class MomentumReflowUiTests(unittest.TestCase):
    def test_sidebar_description_and_renderer_are_wired(self):
        source = open("web_ui.py", encoding="utf-8").read()
        self.assertIn("['reflow','动能回流'", source)
        self.assertIn("['reflow_1h','1H首次回流'", source)
        self.assertIn("function renderMomentumReflow(", source)
        self.assertIn('mode == "reflow"', source)

    def test_route_starts_reflow_scan_without_touching_trader(self):
        web_ui = importlib.import_module("web_ui")
        payload = {"rows": [], "scanned": 2, "errors": 0, "initialized": 2}
        with patch.object(web_ui, "scan_momentum_reflow", return_value=payload):
            client = web_ui.app.test_client()
            response = client.get("/scan/reflow/1h")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["scanning"])
```

Use a bounded wait helper in the route test to wait until `web_ui.state["scanning"]` becomes false, then assert `web_ui.cache["reflow_1h"] == payload`.

- [ ] **Step 2: Run integration tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow_integration.py" -v
```

Expected: failures for missing menu, renderer, and scan import.

- [ ] **Step 3: Wire the backend scan into the existing controller**

At module imports:

```python
from pathlib import Path

from momentum_reflow import scan_momentum_reflow
```

Add:

```python
MOMENTUM_REFLOW_LEDGER = Path(__file__).with_name("momentum_reflow_state.json")
```

Add `reflow_1h` to the initial cache. In the existing asynchronous route worker add:

```python
elif mode == "reflow" and interval == "1h":
    cache[key] = scan_momentum_reflow(
        MOMENTUM_REFLOW_LEDGER,
        progress=lambda completed, total: state.update(
            progress=f"{completed}/{total}"
        ),
    )
```

Reject unsupported reflow intervals with HTTP 400 rather than silently routing them to another scanner.

- [ ] **Step 4: Add the menu and dedicated renderer**

Add the group:

```javascript
['reflow','动能回流',[['reflow_1h','1H首次回流','M']]],
```

Add a concise `_desc.reflow_1h` explaining strong EMA50 breakout, expansion, first return, and daily confirmation.

In `render`, detect `tab === 'reflow_1h'` and call `renderMomentumReflow(rows)` before the generic table path. The renderer must:

- accept the payload object, not assume an array;
- show scanned and error counts in the stat bar;
- split LONG and SHORT with existing green/red direction styling;
- escape every payload-derived string with the existing HTML escaping helper;
- format window index as `N/5`;
- format close distance and max expansion to two decimals with `R`-style ATR labels;
- show a clear empty state when `payload.rows` is empty;
- preserve the existing copy-symbol behavior.

Use these exact daily labels:

```javascript
var dailyLabel={
  strong_momentum:'强动能日K',
  bullish_engulfing:'看涨吞没',
  bearish_engulfing:'看跌吞没',
  hammer:'锤子线',
  shooting_star:'流星线',
  morning_star:'早晨之星',
  evening_star:'黄昏之星',
  bottom_fractal:'底分型',
  top_fractal:'顶分型'
};
```

- [ ] **Step 5: Add a Node rendering regression**

Extract and evaluate the actual renderer factory or pure HTML helper from `web_ui.py`, following the established pattern in `tests/test_r_performance_integration.py`. Assert:

- LONG and SHORT labels render;
- `3/5`, `0.18 ATR`, daily label, and breakout volume ratio render;
- symbol and daily kind payloads are escaped;
- empty payload renders the dedicated empty message;
- `null`, `NaN`, and infinite numeric values do not appear as literal UI text.

- [ ] **Step 6: Run focused integration tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow_integration.py" -v
```

Expected: all UI and route integration tests pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add web_ui.py tests/test_momentum_reflow_integration.py
git commit -m "feat: add momentum reflow scan dashboard"
```

---

### Task 5: Full verification and deployment gate

**Files:**
- Modify after verified deployment only: `PROGRESS.md`

**Interfaces:**
- Consumes the completed local feature.
- Produces fresh build, test, browser, and deployment evidence.

- [ ] **Step 1: Run focused and full local verification**

Run:

```powershell
python -m unittest discover -s tests -p "test_momentum_reflow*.py" -v
python -m unittest discover -s tests -v
python -m py_compile momentum_reflow.py web_ui.py
git diff --check
git status --short
```

Expected:

- all focused tests pass;
- the full test suite reports zero failures and zero errors;
- both files compile;
- `git diff --check` emits no output;
- the worktree is clean after the final verification commit.

- [ ] **Step 2: Review scope before deployment**

Run:

```powershell
git diff HEAD~4..HEAD -- momentum_reflow.py web_ui.py tests/test_momentum_reflow.py tests/test_momentum_reflow_integration.py
```

Verify:

- `trader.py`, `admin_server.py`, configuration, positions, trades, and PnL files are unchanged;
- no secret, API key, state file, or generated browser artifact is tracked;
- the new route is read-only and only invokes the new scanner.

- [ ] **Step 3: Obtain explicit deployment approval**

Report the exact files proposed for upload:

- `momentum_reflow.py`
- `web_ui.py`

Explain that deployment backs up server files and restarts only `macd-bot`. Wait for the user's explicit approval before any server write or restart.

- [ ] **Step 4: Back up and deploy after approval**

On the server, create a timestamped backup containing:

- current `<deploy-dir>/web_ui.py`;
- current `<deploy-dir>/momentum_reflow.py` if it exists;
- current `<deploy-dir>/momentum_reflow_state.json` if it exists;
- current demo configuration, positions, and trade records for before/after integrity comparison.

Upload only `momentum_reflow.py` and `web_ui.py`. Compile both on the server before restarting:

```bash
cd <deploy-dir>
python3 -m py_compile momentum_reflow.py web_ui.py
systemctl restart macd-bot
systemctl is-active macd-bot
systemctl is-active macd-admin
```

Expected: compilation succeeds and both services return `active`. Do not restart `macd-admin`.

- [ ] **Step 5: Verify the live feature**

Use a real authenticated browser to:

1. open the left menu;
2. select “动能回流 → 1H首次回流”;
3. confirm the scan button is visible;
4. start a scan;
5. verify progress and completion states;
6. verify empty state or populated table;
7. verify mobile sidebar behavior;
8. check console errors and network status.

On the server:

- compare local and remote SHA256 for both deployed files;
- verify logs after restart contain no `Traceback`, `ImportError`, `SyntaxError`, or `ModuleNotFoundError`;
- verify existing positions, configuration, trade count, and PnL files are unchanged;
- verify `momentum_reflow_state.json` is created only by the first successful scan and contains version 1.

- [ ] **Step 6: Record verified deployment**

Append a dated `PROGRESS.md` section containing:

- strategy rules implemented;
- files deployed;
- backup path;
- service restart scope;
- local and server hashes;
- focused and full test counts;
- browser verification;
- state-ledger status;
- confirmation that trading data was unchanged.

Commit:

```powershell
git add PROGRESS.md
git commit -m "docs: record momentum reflow deployment"
```
