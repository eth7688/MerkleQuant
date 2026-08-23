# Predicta Weak-Trend Choppy Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend only the Predicta entry filter so a signal is blocked when the existing compression filter fires or when both ADX(14) is below 18 and ER(20) is below 0.20.

**Architecture:** Keep `evaluate_choppy_market_adaptive()` unchanged for RJ and add a Predicta-specific wrapper in `strategy_filters.py`. Route only `_predicta_choppy_filter_state()` through the wrapper, persist the new audit metrics in signal/position snapshots, and verify behavior with a frozen DEXE 30-minute fixture plus isolation tests.

**Tech Stack:** Python 3.12, pandas, NumPy, Flask trading engine, `unittest`, JSON fixtures, Binance closed-candle data used only to capture the frozen regression fixture.

## Global Constraints

- Predicta final choppy rule is `existing_choppy OR (ADX(14) < 18 AND ER(20) < 0.20)`.
- All calculations are anchored to the closed Predicta signal candle and must not read later or unclosed candles.
- RJ must continue using only `evaluate_choppy_market_adaptive()` with identical behavior.
- Existing positions, stop-loss, take-profit, trailing, exit, PnL data, and API credentials must not be changed.
- Existing `hard/off` toggle semantics remain; no threshold controls and no `log_only` UI are added.
- Invalid or insufficient new metrics fail open to the existing compression result.
- Only local source files are updated before deployment; tests, fixtures, specs, plans, `PROGRESS.md`, and memory files are not uploaded.
- After deployment, update local `PROGRESS.md` with the change, backup path, deployed files, and verification evidence.

---

### Task 1: Freeze the DEXE Regression and Establish RED

**Files:**
- Create: `tests/fixtures/dexeusdt_30m_20260718_signal.json`
- Modify: `tests/test_choppy_filter.py`

**Interfaces:**
- Consumes: Binance closed 30-minute DEXEUSDT candles ending at `ot=1784320200000`.
- Produces: a deterministic fixture and tests for `strategy_filters.evaluate_predicta_choppy_market(df, anchor_idx=None) -> dict[str, Any]`.

- [ ] **Step 1: Read and freeze the real DEXE candle window**

Run a read-only server probe that prints the last 160 closed rows ending at the signal candle:

```python
import json
import pandas as pd
from screener import fetch_klines

target = 1784320200000
frame = fetch_klines("DEXEUSDT", "30m", 240, exchange="binance", closed_only=True)
frame = frame.reset_index(drop=True)
anchor = frame.index[pd.to_numeric(frame["ot"], errors="coerce") == target][-1]
rows = frame.iloc[max(0, anchor - 159):anchor + 1][["ot", "o", "h", "l", "c", "v"]]
print(json.dumps(rows.to_dict("records"), ensure_ascii=False, separators=(",", ":")))
```

Use `apply_patch` to store the exact printed JSON as `tests/fixtures/dexeusdt_30m_20260718_signal.json`. Verify the last row has `ot=1784320200000` and `c=36.121`.

- [ ] **Step 2: Add the failing Predicta-specific tests**

```python
import json
import strategy_filters

FIXTURES = Path(__file__).resolve().parent / "fixtures"

def _dexe_frame():
    rows = json.loads((FIXTURES / "dexeusdt_30m_20260718_signal.json").read_text(encoding="utf-8"))
    return pd.DataFrame(rows)

class PredictaWeakTrendChoppyTest(unittest.TestCase):
    def test_real_dexe_signal_is_weak_directional_chop(self):
        self.assertTrue(hasattr(strategy_filters, "evaluate_predicta_choppy_market"))
        state = strategy_filters.evaluate_predicta_choppy_market(_dexe_frame())
        self.assertTrue(state["choppy_filter_is_choppy"])
        self.assertIn("weak_directional_efficiency", state["choppy_filter_reasons"])
        self.assertLess(state["choppy_adx"], 18.0)
        self.assertLess(state["choppy_efficiency_ratio"], 0.20)

    def test_directional_expansion_remains_allowed(self):
        frame = _frame(100.0 + np.arange(130) * 0.5)
        state = strategy_filters.evaluate_predicta_choppy_market(frame)
        self.assertFalse(state["choppy_filter_is_choppy"])
        self.assertGreaterEqual(state["choppy_efficiency_ratio"], 0.20)

    def test_later_candles_do_not_change_signal_anchor_result(self):
        frame = _dexe_frame()
        extended = pd.concat([frame, _frame([80.0, 120.0, 70.0])], ignore_index=True)
        expected = strategy_filters.evaluate_predicta_choppy_market(frame)
        actual = strategy_filters.evaluate_predicta_choppy_market(extended, anchor_idx=len(frame) - 1)
        self.assertEqual(actual, expected)
```

- [ ] **Step 3: Run the test and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_choppy_filter.py PredictaWeakTrendChoppyTest -v`

Expected: FAIL at `hasattr(...)` because `evaluate_predicta_choppy_market` does not exist.

---

### Task 2: Implement the Predicta-Only Indicator

**Files:**
- Modify: `strategy_filters.py`
- Test: `tests/test_choppy_filter.py`

**Interfaces:**
- Consumes: a closed OHLC DataFrame and optional signal-candle index.
- Produces: `evaluate_predicta_choppy_market()` returning the existing choppy state plus `choppy_adx_period`, `choppy_adx`, `choppy_efficiency_period`, and `choppy_efficiency_ratio`.

- [ ] **Step 1: Add minimal ADX and ER helpers**

```python
def _ema_rma(values: pd.Series, length: int) -> pd.Series:
    return values.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def _adx_value(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float | None:
    previous = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    atr = _ema_rma(true_range, period)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_di = 100.0 * _ema_rma(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * _ema_rma(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    value = float(_ema_rma(dx, period).iloc[-1])
    return value if np.isfinite(value) else None


def _efficiency_ratio(close: pd.Series, period: int) -> float | None:
    if len(close) < period + 1:
        return None
    window = close.iloc[-period - 1:]
    path = float(window.diff().abs().sum())
    if not np.isfinite(path) or path <= 0:
        return None
    value = abs(float(window.iloc[-1] - window.iloc[0])) / path
    return value if np.isfinite(value) else None
```

- [ ] **Step 2: Add the Predicta-specific wrapper**

```python
def evaluate_predicta_choppy_market(
    df: pd.DataFrame,
    anchor_idx: int | None = None,
    adx_period: int = 14,
    efficiency_period: int = 20,
    adx_threshold: float = 18.0,
    efficiency_threshold: float = 0.20,
) -> dict[str, Any]:
    state = dict(evaluate_choppy_market_adaptive(df, anchor_idx=anchor_idx))
    state.update({
        "choppy_adx_period": adx_period,
        "choppy_adx": None,
        "choppy_efficiency_period": efficiency_period,
        "choppy_efficiency_ratio": None,
    })
    if not state.get("choppy_filter_available"):
        return state
    resolved_idx = len(df) - 1 if anchor_idx is None else int(anchor_idx)
    if resolved_idx < 0:
        resolved_idx += len(df)
    try:
        context = df.iloc[:resolved_idx + 1]
        high = _column(context, "h", "high")
        low = _column(context, "l", "low")
        close = _column(context, "c", "close")
        adx = _adx_value(high, low, close, adx_period)
        efficiency = _efficiency_ratio(close, efficiency_period)
    except (KeyError, TypeError, ValueError, IndexError):
        return state
    state["choppy_adx"] = adx
    state["choppy_efficiency_ratio"] = efficiency
    if adx is not None and efficiency is not None and adx < adx_threshold and efficiency < efficiency_threshold:
        reasons = list(state.get("choppy_filter_reasons") or [])
        if "weak_directional_efficiency" not in reasons:
            reasons.append("weak_directional_efficiency")
        state["choppy_filter_is_choppy"] = True
        state["choppy_filter_reasons"] = reasons
        if state.get("choppy_filter_reason") == "pass":
            state["choppy_filter_reason"] = "weak_directional_efficiency"
    return state
```

- [ ] **Step 3: Add combination and fail-open tests**

Use deterministic OHLC frames and optional thresholds to verify AND semantics:

```python
def test_weak_trend_requires_both_adx_and_efficiency(self):
    frame = _dexe_frame()
    adx_only = strategy_filters.evaluate_predicta_choppy_market(
        frame, efficiency_threshold=0.01
    )
    efficiency_only = strategy_filters.evaluate_predicta_choppy_market(
        frame, adx_threshold=1.0
    )
    self.assertNotIn("weak_directional_efficiency", adx_only["choppy_filter_reasons"])
    self.assertNotIn("weak_directional_efficiency", efficiency_only["choppy_filter_reasons"])

def test_invalid_efficiency_keeps_existing_result(self):
    frame = _frame([100.0] * 130)
    state = strategy_filters.evaluate_predicta_choppy_market(frame)
    self.assertTrue(state["choppy_filter_is_choppy"])
    self.assertIsNone(state["choppy_efficiency_ratio"])
    self.assertNotIn("weak_directional_efficiency", state["choppy_filter_reasons"])
```

- [ ] **Step 4: Run focused indicator tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_choppy_filter.py PredictaWeakTrendChoppyTest ChoppyFilterModuleTest -v`

Expected: all selected tests pass; the real DEXE fixture contains `weak_directional_efficiency`, and existing directional expansion still passes.

---

### Task 3: Route Only Predicta and Persist Audit Fields

**Files:**
- Modify: `trader.py`
- Modify: `tests/test_choppy_filter.py`
- Modify: `tests/test_predicta_pipeline.py`

**Interfaces:**
- Consumes: `evaluate_predicta_choppy_market()` from Task 2.
- Produces: Predicta-only routing plus persisted metrics in signal events and `Position.choppy_filter`.

- [ ] **Step 1: Add failing isolation and audit tests**

```python
def test_predicta_uses_enhanced_filter_while_rj_keeps_base_filter(self):
    bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
    bot.cfg = TradeConfig(predicta_choppy_filter_mode="hard", rj_choppy_filter_mode="hard")
    frame = _dexe_frame()
    self.assertTrue(bot._predicta_choppy_filter_state(frame, len(frame) - 1)["choppy_filter_is_choppy"])
    self.assertFalse(bot._rj_choppy_filter_state(frame, len(frame) - 1)["choppy_filter_is_choppy"])

def test_position_audit_keeps_predicta_direction_metrics(self):
    bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
    audit = bot._position_choppy_filter({
        "choppy_filter_mode": "hard",
        "choppy_filter_available": True,
        "choppy_filter_is_choppy": False,
        "choppy_filter_reason": "pass",
        "choppy_adx_period": 14,
        "choppy_adx": 24.5,
        "choppy_efficiency_period": 20,
        "choppy_efficiency_ratio": 0.31,
    })
    self.assertEqual(audit["adx_period"], 14)
    self.assertAlmostEqual(audit["adx"], 24.5)
    self.assertEqual(audit["efficiency_period"], 20)
    self.assertAlmostEqual(audit["efficiency_ratio"], 0.31)
```

Add a Predicta pipeline test that uses the frozen DEXE frame and asserts `hard` returns no fast/waiting candidates while `off` still returns the original candidate path.

```python
def test_real_dexe_weak_trend_is_blocked_only_in_hard_mode(self):
    rows = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "dexeusdt_30m_20260718_signal.json")
        .read_text(encoding="utf-8")
    )
    frame = pd.DataFrame(rows)
    self.bot.cfg.predicta_choppy_filter_mode = "hard"
    hard_fast, hard_waiting = self.bot._predicta_candidates_from_df("DEXEUSDT", "30m", frame)
    self.bot.cfg.predicta_choppy_filter_mode = "off"
    off_fast, off_waiting = self.bot._predicta_candidates_from_df("DEXEUSDT", "30m", frame)
    self.assertEqual(hard_fast, [])
    self.assertEqual(hard_waiting, [])
    self.assertEqual(len(off_fast), 1)
    self.assertEqual(off_waiting, [])
```

- [ ] **Step 2: Run the new integration tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_choppy_filter.py PredictaWeakTrendChoppyTest.test_predicta_uses_enhanced_filter_while_rj_keeps_base_filter PredictaWeakTrendChoppyTest.test_position_audit_keeps_predicta_direction_metrics -v`

Expected: FAIL because Predicta still calls the base evaluator and position audit omits the four new fields.

- [ ] **Step 3: Route Predicta through the dedicated evaluator**

Import `evaluate_predicta_choppy_market` in `trader.py`. Extend `_choppy_filter_state()` with an optional evaluator defaulting to `evaluate_choppy_market_adaptive`, then call it only from Predicta:

```python
def _choppy_filter_state(self, df, anchor_idx: int, mode: str, evaluator=evaluate_choppy_market_adaptive) -> dict:
    # Keep existing mode normalization and off return unchanged.
    state = evaluator(df, anchor_idx=anchor_idx)
    state["choppy_filter_mode"] = mode
    state["choppy_filter_anchor"] = "signal_key"
    return state

def _predicta_choppy_filter_state(self, df, anchor_idx: int) -> dict:
    return self._choppy_filter_state(
        df,
        anchor_idx,
        getattr(self.cfg, "predicta_choppy_filter_mode", "hard"),
        evaluator=evaluate_predicta_choppy_market,
    )
```

Do not change `_rj_choppy_filter_state()`.

- [ ] **Step 4: Persist new audit fields**

Extend `_position_choppy_filter()` to return:

```python
"adx_period": optional_int("adx_period", "choppy_adx_period"),
"adx": optional_float("adx", "choppy_adx"),
"efficiency_period": optional_int("efficiency_period", "choppy_efficiency_period"),
"efficiency_ratio": optional_float("efficiency_ratio", "choppy_efficiency_ratio"),
```

Define `optional_int()` beside the existing `optional_float()`:

```python
def optional_int(name, source_name):
    value = state.get(name, state.get(source_name))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
```

Add the raw `choppy_adx_period`, `choppy_adx`, `choppy_efficiency_period`, and `choppy_efficiency_ratio` names to `_signal_snapshot()` so events and positions receive the signal-time values. Missing fields remain `None`; old records are never backfilled from current candles.

- [ ] **Step 5: Run focused integration tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; python tests/test_choppy_filter.py tests/test_predicta_pipeline.py -v`

Expected: all choppy and Predicta pipeline tests pass; DEXE is blocked only in Predicta `hard` mode, RJ remains unchanged, and `off` still allows the signal.

- [ ] **Step 6: Commit the implementation**

```powershell
git add strategy_filters.py trader.py tests/test_choppy_filter.py tests/test_predicta_pipeline.py tests/fixtures/dexeusdt_30m_20260718_signal.json
git commit -m "fix: filter weak-trend Predicta entries"
```

---

### Task 4: Complete Local Verification

**Files:**
- Verify: `strategy_filters.py`
- Verify: `trader.py`
- Verify: `tests/`

**Interfaces:**
- Consumes: all committed implementation and regression files.
- Produces: fresh compile, full regression, DEXE replay, and clean-diff evidence.

- [ ] **Step 1: Compile affected modules**

Run: `python -m py_compile strategy_filters.py trader.py web_ui.py admin_server.py`

Expected: exit code 0 with no output.

- [ ] **Step 2: Run the complete suite**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m unittest discover -s tests -p 'test_*.py'`

Expected: all tests pass with zero failures.

- [ ] **Step 3: Replay the frozen DEXE signal**

Run an inline Python probe that loads the fixture, calls `evaluate_predicta_choppy_market()`, and calls `_predicta_candidates_from_df()` with `predicta_choppy_filter_mode="hard"`.

Expected: state reason includes `weak_directional_efficiency`; both fast and waiting candidate lists are empty.

- [ ] **Step 4: Check scope and whitespace**

Run: `git diff --check; git status --short`

Expected: no whitespace errors and no unrelated files.

---

### Task 5: Back Up, Deploy, Verify, and Record Progress

**Files:**
- Deploy: `strategy_filters.py` to `<deploy-dir>/strategy_filters.py`
- Deploy: `trader.py` to `<deploy-dir>/trader.py`
- Preserve: `<deploy-dir>/demo_bot_config.json`
- Modify locally: `PROGRESS.md`

**Interfaces:**
- Consumes: SSH access to `root@<production-host>` and systemd service `macd-bot`.
- Produces: active Predicta weak-trend filtering with unchanged configuration, positions, and credentials.

- [ ] **Step 1: Create a timestamped server backup**

Back up `<deploy-dir>/trader.py`, `strategy_filters.py`, and `demo_bot_config.json` into `<deploy-dir>/backups/predicta_weak_trend_<timestamp>/` and print the directory listing.

- [ ] **Step 2: Upload only runtime files**

```powershell
scp -i .\<ssh-key> .\strategy_filters.py .\trader.py root@<production-host>:<deploy-dir>/
```

Expected: exit code 0; fixtures, tests, docs, progress, and memory are not uploaded.

- [ ] **Step 3: Compile and restart only the trading service**

Run server-side `./venv/bin/python3 -m py_compile strategy_filters.py trader.py web_ui.py admin_server.py`, restart `macd-bot`, and query `systemctl is-active macd-bot macd-admin`.

Expected: compile succeeds and both services report `active`.

- [ ] **Step 4: Verify deployment without exposing secrets or mutating positions**

Verify local/server SHA256 for both deployed files, then print only:

```python
{
    "predicta_choppy_filter_mode": cfg.get("predicta_choppy_filter_mode"),
    "api_key_present": bool(cfg.get("testnet_api_key")),
    "api_secret_present": bool(cfg.get("testnet_api_secret")),
    "running": status.get("running"),
    "source": status.get("entry_signal_source"),
    "positions": status.get("positions"),
}
```

Expected: mode remains `hard`, both credential-presence flags are true, engine is running with `predicta_ewo`, and the position count is unchanged from the pre-deploy snapshot.

- [ ] **Step 5: Run a non-ordering server DEXE probe**

Load the frozen historical window or reconstruct the exact window ending at `1784320200000`, call `evaluate_predicta_choppy_market()`, and print only the reason, ADX, ER, and boolean result.

Expected: `weak_directional_efficiency`, ADX below 18, ER below 0.20, and `choppy_filter_is_choppy=True`.

- [ ] **Step 6: Update and commit `PROGRESS.md`**

Record the DEXE root cause, exact new rule, local test totals, backup path, deployed hashes/files, service status, retained `hard` mode, credential-presence verification, unchanged pre/post position count, and server DEXE probe result.

```powershell
git add PROGRESS.md
git commit -m "docs: record Predicta weak-trend deployment"
```

- [ ] **Step 7: Perform final verification**

Re-run the full local suite, `py_compile`, `git diff --check`, `git status --short`, service status, runtime source/mode, file hashes, and non-secret credential-presence checks.

Expected: all tests pass, working tree is clean, both services are active, hashes match, and live configuration remains intact.
