# Momentum Compression Compact Range Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject ARCUSDT-style one-sided, wide, strongly drifting channels while preserving valid strict and watch compression candidates.

**Architecture:** Extend the existing shared geometry evaluator in `momentum_compression.py`; both `STRICT` and `WATCH` consume the same computed dual-boundary touch, terminal width, and midline-drift metrics. Preserve the state machine and alert pipeline, and prove behavior with a real 220-candle Binance Futures ARC fixture plus threshold unit tests.

**Tech Stack:** Python 3.12, pandas, numpy, unittest, Flask service deployment through systemd.

## Global Constraints

- LONG requires at least 3 lower-boundary touches and 2 upper-boundary touches; SHORT requires at least 3 upper-boundary touches and 2 lower-boundary touches.
- `channel_width_atr <= 3.0` and `midline_drift_atr <= 4.0` are inclusive thresholds.
- `STRICT` and `WATCH` share every new geometry rule; WATCH only retains its existing three-candle EMA relaxation.
- Do not modify the trading engine, automatic orders, breakout state machine, alerts, volume threshold, or Binance Futures data source.
- Do not send a test WeChat message or fabricate a breakout event.
- Update local files before production deployment and update local `PROGRESS.md` after verified deployment.

---

### Task 1: Add the real ARC historical regression fixture

**Files:**
- Create: `tests/fixtures/arcusdt_binance_futures_15m_20260825_2215.json`
- Modify: `tests/test_momentum_compression.py`

**Interfaces:**
- Consumes: Binance Futures `GET /fapi/v1/klines`, `evaluate_side(symbol, side, frame, live_price, evaluated_at_ms, htf_alignment)`.
- Produces: immutable ARC fixture with SHA256 `2e759524f63b70d777e706fea737d58db0ab9f5df6ece446d285f2461c6e1072` and a failing regression test.

- [ ] **Step 1: Save the verified 220-candle ARC fixture**

Use `symbol=ARCUSDT`, `interval=15m`, `limit=220`, `endTime=1787668199999`; store numeric OHLCV dictionaries with first open time `1787470200000`, last open time `1787667300000`, `evaluated_at_ms=1787668200000`, `live_price=0.07141`, former window `1787643900000..1787667300000`, and the exact SHA256 above.

- [ ] **Step 2: Write the failing regression test**

```python
ARC_FIXTURE = Path(__file__).parent / "fixtures" / "arcusdt_binance_futures_15m_20260825_2215.json"
ARC_OHLCV_SHA256 = "2e759524f63b70d777e706fea737d58db0ab9f5df6ece446d285f2461c6e1072"

def test_arc_binance_trend_channel_is_rejected(self):
    payload = json.loads(ARC_FIXTURE.read_text(encoding="utf-8"))
    canonical = json.dumps(payload["ohlcv"], sort_keys=True, separators=(",", ":"))
    self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), ARC_OHLCV_SHA256)
    result = evaluate_side(
        payload["symbol"], "SHORT", pd.DataFrame(payload["ohlcv"]),
        payload["live_price"], evaluated_at_ms=payload["evaluated_at_ms"],
        htf_alignment="UNKNOWN",
    )
    self.assertEqual(result["state"], "REJECTED")
    self.assertTrue({
        "INSUFFICIENT_OPPOSITE_TOUCHES", "CHANNEL_TOO_WIDE",
        "CHANNEL_DRIFT_TOO_LARGE",
    }.intersection(result["watch_rejection_reasons"]))
```

- [ ] **Step 3: Run the regression test and verify RED**

Run: `python -m unittest tests.test_momentum_compression.CompressionHistoricalRegressionTests.test_arc_binance_trend_channel_is_rejected -v`

Expected: FAIL because current output is `COMPRESSION_ACTIVE_SHORT`, proving the test catches the production false positive.

- [ ] **Step 4: Commit the failing regression fixture and test**

```text
git add tests/fixtures/arcusdt_binance_futures_15m_20260825_2215.json tests/test_momentum_compression.py
git commit -m "test: reproduce ARC compression false positive"
```

---

### Task 2: Implement shared compact-range geometry guards

**Files:**
- Modify: `momentum_compression.py`
- Modify: `tests/test_momentum_compression.py`

**Interfaces:**
- Consumes: `_touch_events(values, boundary, atr, tolerance)` and the existing fitted `upper`, `lower`, `upper_slope`, `lower_slope` envelope.
- Produces: `opposite_events`, `channel_width_atr`, and `midline_drift_atr` in rule metrics and serialized accepted evaluations.

- [ ] **Step 1: Add failing parameter and exact-threshold unit tests**

Assert these defaults:

```python
self.assertEqual(params.min_opposite_boundary_touches, 2)
self.assertEqual(params.max_channel_width_atr, 3.0)
self.assertEqual(params.max_midline_drift_atr, 4.0)
```

Add focused `_non_length_rules` tests using prepared frames with controlled `atr14` and patched common geometry to verify:

```python
self.assertIn("INSUFFICIENT_OPPOSITE_TOUCHES", one_opposite["rejection_reasons"])
self.assertNotIn("INSUFFICIENT_OPPOSITE_TOUCHES", two_opposite["rejection_reasons"])
self.assertNotIn("CHANNEL_TOO_WIDE", exactly_three_atr["rejection_reasons"])
self.assertIn("CHANNEL_TOO_WIDE", above_three_atr["rejection_reasons"])
self.assertNotIn("CHANNEL_DRIFT_TOO_LARGE", exactly_four_atr["rejection_reasons"])
self.assertIn("CHANNEL_DRIFT_TOO_LARGE", above_four_atr["rejection_reasons"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_momentum_compression.CompressionIndicatorTests.test_default_parameters_match_approved_spec tests.test_momentum_compression.CompressionRuleTests -v`

Expected: FAIL on missing parameters and missing rejection semantics.

- [ ] **Step 3: Add the minimal parameters and shared calculations**

Extend `CompressionParams`:

```python
min_opposite_boundary_touches: int = 2
max_channel_width_atr: float = 3.0
max_midline_drift_atr: float = 4.0
```

Inside `_non_length_rules`, compute both boundaries regardless of side, then map direction/opposite:

```python
upper_events = _touch_events(frame["h"], upper, atr, params.touch_tolerance_atr)
lower_events = _touch_events(frame["l"], lower, atr, params.touch_tolerance_atr)
directional_events, opposite_events = (
    (lower_events, upper_events) if side == "LONG" else (upper_events, lower_events)
)
channel_width_atr = (
    float(upper.iloc[-1] - lower.iloc[-1]) / last_atr
    if math.isfinite(last_atr) and last_atr > 0 else float("inf")
)
midline_slope = (envelope["upper_slope"] + envelope["lower_slope"]) / 2
midline_drift_atr = (
    abs(float(midline_slope)) * (len(frame) - 1) / last_atr
    if math.isfinite(last_atr) and last_atr > 0 else float("inf")
)
```

Append rejection reasons using inclusive pass boundaries:

```python
if len(opposite_events) < params.min_opposite_boundary_touches:
    reasons.append("INSUFFICIENT_OPPOSITE_TOUCHES")
if not math.isfinite(channel_width_atr) or channel_width_atr > params.max_channel_width_atr:
    reasons.append("CHANNEL_TOO_WIDE")
if not math.isfinite(midline_drift_atr) or midline_drift_atr > params.max_midline_drift_atr:
    reasons.append("CHANNEL_DRIFT_TOO_LARGE")
```

Return all three metrics from `_non_length_rules`.

- [ ] **Step 4: Serialize audit metrics without changing state identity**

In `_accepted_evaluation`, add:

```python
"opposite_touch_count": len(metrics["opposite_events"]),
"opposite_touch_times": [
    int(window["ot"].iloc[index]) for index in metrics["opposite_events"]
],
"channel_width_atr": metrics["channel_width_atr"],
"midline_drift_atr": metrics["midline_drift_atr"],
```

In `_rejected`, add safe empty/default audit values. Do not add any of these fields to `compression_identity`.

- [ ] **Step 5: Update the legacy oracle used by equivalence tests**

Mirror the approved geometry calculations in the test-only legacy evaluator so optimization equivalence tests compare execution paths under the new rules rather than obsolete behavior.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `python -m unittest tests.test_momentum_compression -v`

Expected: all tests pass, including the ARC historical regression.

- [ ] **Step 7: Commit the production fix**

```text
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "fix: reject non-compact compression channels"
```

---

### Task 3: Verify compatibility and real-snapshot behavior

**Files:**
- Modify only if a failing compatibility assertion proves it necessary: `tests/test_momentum_compression_store.py`
- Read: `momentum_compression_store.py`, `momentum_compression_service.py`, `momentum_compression_monitor.py`

**Interfaces:**
- Consumes: accepted/rejected evaluation dictionaries from Task 2.
- Produces: evidence that old persisted rows remain readable and current scan coordination remains unchanged.

- [ ] **Step 1: Run all compression tests**

Run: `python -m unittest discover -s tests -p "test_momentum_compression*.py" -v`

Expected: zero failures and zero errors.

- [ ] **Step 2: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: zero failures and zero errors.

- [ ] **Step 3: Compile all modified runtime modules**

Run: `python -m py_compile momentum_compression.py momentum_compression_service.py momentum_compression_monitor.py web_ui.py`

Expected: exit code 0 with no output.

- [ ] **Step 4: Recompute the ARC fixture and inspect exact diagnostics**

Run the historical regression directly and print the SHORT rejection reasons, opposite touch count, channel width ATR, and midline drift ATR.

Expected: `REJECTED`; diagnostics show the fixture no longer enters STRICT or WATCH.

- [ ] **Step 5: Review the final diff**

Run: `git diff HEAD~2 --check` and inspect `git diff HEAD~2 -- momentum_compression.py tests/test_momentum_compression.py tests/fixtures/arcusdt_binance_futures_15m_20260825_2215.json`.

Expected: no unrelated source, UI, alert, trade, or configuration changes.

---

### Task 4: Back up, deploy, and record production verification

**Files:**
- Deploy: `momentum_compression.py`
- Modify after deployment: `PROGRESS.md`

**Interfaces:**
- Consumes: verified local commit from Tasks 1–3 and SSH key `<ssh-key>`.
- Produces: backed-up production file, active `macd-bot` service, matching deployed SHA256, and a concrete local deployment record.

- [ ] **Step 1: Resolve and verify production targets read-only**

Confirm host `root@<production-host>`, application directory `<deploy-dir>`, service `macd-bot`, and current remote file hashes before any write.

- [ ] **Step 2: Create a timestamped server backup**

Back up `momentum_compression.py`, `momentum_compression_state.json`, `momentum_compression_events.jsonl`, and `momentum_compression_alert_state.json` into a new explicit archive under `/root`; do not overwrite the prior backup.

- [ ] **Step 3: Upload to a temporary file and verify it**

Upload local `momentum_compression.py` as `<deploy-dir>/momentum_compression.py.codex-new`, run server-venv `py_compile`, and compare SHA256 with local before replacement.

- [ ] **Step 4: Replace and restart only macd-bot**

Move the verified temporary file over `momentum_compression.py`, restart `macd-bot`, and do not restart `macd-admin`.

- [ ] **Step 5: Verify production evidence**

Confirm `systemctl is-active macd-bot` returns `active`, deployed SHA256 matches local, HTTP page/API responds, service logs contain no new traceback, live price state is connected, and the monitor resumes scanning. Do not send test alerts.

- [ ] **Step 6: Update and commit PROGRESS.md**

Record the exact rule changes, deployed file, backup archive path, service action, local/remote SHA256, test totals, and post-restart verification result.

```text
git add PROGRESS.md
git commit -m "docs: record compression compact guard deployment"
```
