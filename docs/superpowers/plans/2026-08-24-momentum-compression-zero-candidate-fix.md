# Momentum Compression Zero-Candidate Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align G04 with the approved EMA-band rule, return truthful rejected windows, and expose useful rejection counts without weakening any other compression threshold.

**Architecture:** Keep the pure rule correction inside `momentum_compression.py`, preserve scan eligibility semantics in `momentum_compression_service.py`, and reuse the existing scan callback/global status path in `web_ui.py`. Add only regression tests that reproduce the production zero-candidate diagnostics and verify the public/internal status contracts.

**Tech Stack:** Python 3.12, pandas, NumPy, Flask, native HTML/CSS/JavaScript, `unittest`.

## Global Constraints

- Continue using Binance Futures closed 15m candles and the `>= 1,000,000 USDT` quote-volume universe.
- Do not change pivot span, touch tolerance/count, contraction ratio, EMA distance, swing structure, 15–100 bar limits, HTF labels, state transitions, alerts, or trading logic.
- Use test-first RED/GREEN cycles for every production change.
- Do not persist all rejected rows or modify state-file schemas.
- Production deployment requires separate user confirmation after local verification.

---

### Task 1: Correct G04 and rejected-window selection

**Files:**
- Modify: `momentum_compression.py:145-243`
- Test: `tests/test_momentum_compression.py`

**Interfaces:**
- Consumes: `_non_length_rules(frame, side, params, common=None)` and `_maximal_structural_suffix(indicators, side, params, common_cache=None)`.
- Produces: unchanged public `evaluate_side(symbol, side, closed_15m, live_price, *, evaluated_at_ms, htf_alignment, params) -> dict` and `evaluate_both_sides(symbol, closed_15m, live_price, *, evaluated_at_ms, htf_alignment_by_side, params) -> list[dict]` contracts with truthful `compression_bars` and `rejection_reasons`.

- [ ] **Step 1: Write failing G04 tests**

Add focused tests proving a LONG window whose EMA order is valid and whose closes are below both EMAs is not rejected by `CLOSE_IN_EMA_BAND`, while a close inside or equal to either EMA boundary is rejected. Add mirrored SHORT coverage.

```python
def test_g04_accepts_close_on_either_side_of_ordered_ema_band(self):
    frame = add_compression_indicators(valid_compression_frame(15))
    frame["ema8"], frame["ema21"], frame["c"] = 100.0, 99.0, 98.0
    metrics = _non_length_rules(frame, "LONG", CompressionParams())
    self.assertNotIn("CLOSE_IN_EMA_BAND", metrics["rejection_reasons"])

def test_g04_rejects_inside_and_equal_ema_band_closes(self):
    frame = add_compression_indicators(valid_compression_frame(15))
    frame["ema8"], frame["ema21"], frame["c"] = 100.0, 99.0, 100.0
    metrics = _non_length_rules(frame, "LONG", CompressionParams())
    self.assertIn("CLOSE_IN_EMA_BAND", metrics["rejection_reasons"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression.CompressionRuleTests.test_g04_accepts_close_on_either_side_of_ordered_ema_band tests.test_momentum_compression.CompressionRuleTests.test_g04_rejects_inside_and_equal_ema_band_closes -v
```

Expected: the outside-on-adverse-side assertion fails because current code requires the directional side.

- [ ] **Step 3: Implement the minimum G04 correction**

Compute the inclusive EMA band once and reject only when any close lies inside it:

```python
ema_low = pd.concat((ema8, ema21), axis=1).min(axis=1)
ema_high = pd.concat((ema8, ema21), axis=1).max(axis=1)
if bool(((frame["c"] >= ema_low) & (frame["c"] <= ema_high)).any()):
    reasons.append("CLOSE_IN_EMA_BAND")
```

Keep side-specific EMA ordering and boundary-touch direction unchanged. Update `_ema_candidate_start` to use the same EMA-order plus outside-band predicate so pruning remains output-equivalent.

- [ ] **Step 4: Write failing rejected-window tests**

Add tests for these exact outcomes:

```python
def test_failed_suffix_search_returns_best_real_suffix_not_full_history(self):
    indicators = add_compression_indicators(valid_compression_frame(220))
    indicators["ema8"], indicators["ema21"], indicators["c"] = 100.0, 99.0, 101.0
    def rules(candidate, side, params, common=None):
        reasons = ["INSUFFICIENT_CONTRACTION"] if len(candidate) == 40 else [
            "INSUFFICIENT_CONTRACTION", "INSUFFICIENT_DIRECTIONAL_TOUCHES"
        ]
        return {"rejection_reasons": reasons}
    with patch.object(compression_module, "_non_length_rules", side_effect=rules), patch.object(
        compression_module, "_common_structure", return_value={}
    ):
        window, metrics = _maximal_structural_suffix(indicators, "LONG", CompressionParams())
    self.assertEqual(len(window), 40)
    self.assertEqual(metrics["rejection_reasons"], ["INSUFFICIENT_CONTRACTION"])

def test_short_ema_streak_reports_its_real_length(self):
    indicators = add_compression_indicators(valid_compression_frame(30))
    indicators["ema8"], indicators["ema21"], indicators["c"] = 101.0, 100.0, 102.0
    indicators.loc[18:, ["ema8", "ema21", "c"]] = [99.0, 100.0, 98.0]
    with patch.object(compression_module, "_non_length_rules", return_value={"rejection_reasons": ["INSUFFICIENT_PIVOTS"]}), patch.object(
        compression_module, "_common_structure", return_value={}
    ):
        window, _ = _maximal_structural_suffix(indicators, "SHORT", CompressionParams())
    self.assertEqual(len(window), 12)
```

- [ ] **Step 5: Run rejected-window tests and verify RED**

Expected: current fallback returns the complete 220-row input.

- [ ] **Step 6: Implement truthful fallback selection**

During suffix search, retain the best evaluated candidate ordered by:

1. fewest non-length rejection reasons;
2. longest candidate when counts tie.

Return that candidate if no suffix fully passes. When the latest EMA-valid streak is shorter than the structural minimum, return that real streak with its actual reasons. Do not add a new eligible state and do not alter `_evaluate_prepared_side` length enforcement.

- [ ] **Step 7: Run rule tests and commit**

```powershell
python -m unittest tests.test_momentum_compression -q
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "fix: align compression EMA band and suffix diagnostics"
```

Expected: all `test_momentum_compression` tests pass.

---

### Task 2: Preserve rejection counts through monitor and status APIs

**Files:**
- Modify: `momentum_compression_monitor.py:44-94,338-364,463-491`
- Modify: `web_ui.py:495-504,4540-4564`
- Test: `tests/test_momentum_compression_monitor.py`
- Test: `tests/test_momentum_compression_admin.py`

**Interfaces:**
- Produces: `CompressionMonitor.status()["rejection_counts"] -> dict[str, int]`.
- Produces: internal status `scan.rejection_counts -> dict[str, int]` while keeping existing `scanned/eligible/errors` numeric fields.

- [ ] **Step 1: Write failing monitor/API tests**

```python
def test_successful_scan_publishes_rejection_counts(self):
    with TemporaryDirectory() as folder:
        monitor = self._monitor(Path(folder), scan=lambda *_: {
            "evaluated_at": 123,
            "events": [],
            "rejection_counts": {"INSUFFICIENT_PIVOTS": 7},
        })
        self.assertTrue(monitor.scan_now("manual"))
        self.assertEqual(monitor.status()["rejection_counts"], {"INSUFFICIENT_PIVOTS": 7})

def test_loopback_status_includes_scan_rejection_counts(self):
    payload = self.client.get("/internal/compression/status").get_json()
    self.assertEqual(payload["scan"]["rejection_counts"], {"INSUFFICIENT_PIVOTS": 7})
```

Also test that a restart before the first complete scan returns `{}` and a failed scan does not overwrite the last successful distribution.

- [ ] **Step 2: Run focused tests and verify RED**

Run the named new monitor and admin methods with `python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorScanTests.test_successful_scan_publishes_rejection_counts tests.test_momentum_compression_admin.CompressionInternalApiTests.test_loopback_status_includes_scan_rejection_counts -v`.

Expected: `rejection_counts` is absent from monitor/internal status.

- [ ] **Step 3: Implement monitor snapshot**

Initialize `_last_rejection_counts = {}`. On a successful scan, sanitize the report to string keys and non-negative integer values, then replace the snapshot atomically under the existing monitor lifecycle/state lock. Include a copied dictionary in `status()`.

- [ ] **Step 4: Wire internal status without duplicating state**

Build internal `scan.rejection_counts` from `monitor.get("rejection_counts", {})`. Keep the authenticated user endpoint's existing `_compression_rejection_counts` behavior for backward compatibility.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m unittest tests.test_momentum_compression_monitor tests.test_momentum_compression_admin -q
git add momentum_compression_monitor.py web_ui.py tests/test_momentum_compression_monitor.py tests/test_momentum_compression_admin.py
git commit -m "fix: expose compression rejection diagnostics"
```

---

### Task 3: Sort UI diagnostics and guard server scan compatibility

**Files:**
- Modify: `web_ui.py:1981-1995`
- Modify: `momentum_compression_service.py:137-166`
- Test: `tests/test_momentum_compression_integration.py`
- Test: `tests/test_momentum_compression_service.py`

**Interfaces:**
- Keeps: `scan_compression_market(state_path, snapshot_dir, *, progress=None, max_workers=12) -> dict` including `rejection_counts`.
- Keeps: `renderMomentumCompression(payload)` and all existing DOM IDs/classes.

- [ ] **Step 1: Write failing UI ordering test**

Provide `rejection_counts` with deliberately unsorted values and assert the rendered source sorts by count descending, then rejection-code name for deterministic ties. Verify HTML escaping remains in place.

- [ ] **Step 2: Write failing fetch compatibility test**

Patch `momentum_compression_service.fetch_klines` with the production `screener.fetch_klines` signature and call `_scan_symbol`. Assert no unsupported `raise_errors` keyword is supplied while the existing two-attempt retry remains testable through an injected wrapper or a local helper that does not change the shared screener API.

- [ ] **Step 3: Verify RED**

Run the new integration and service tests. Expected: alphabetical UI ordering and/or unsupported keyword use fail the assertions.

- [ ] **Step 4: Implement minimum compatibility changes**

- Sort JavaScript rejection keys with `count DESC`, then `localeCompare` for ties.
- Remove the unsupported `raise_errors=True` argument from the production call path while retaining the existing empty-frame error and retry behavior.
- Do not modify `screener.py` or its public signature.

- [ ] **Step 5: Run compression test suite and commit**

```powershell
python -m unittest discover -s tests -p "test_momentum_compression*.py" -q
git add web_ui.py momentum_compression_service.py tests/test_momentum_compression_integration.py tests/test_momentum_compression_service.py
git commit -m "fix: harden compression scan diagnostics"
```

---

### Task 4: Full verification and handoff

**Files:**
- Verify only; no production edits expected.

- [ ] **Step 1: Run complete tests**

```powershell
python -m unittest discover -s tests -q
```

Expected: all tests pass with zero failures/errors.

- [ ] **Step 2: Compile affected modules**

```powershell
python -m py_compile momentum_compression.py momentum_compression_service.py momentum_compression_monitor.py web_ui.py
```

- [ ] **Step 3: Verify diff hygiene**

```powershell
git diff --check main...HEAD
git status --short
```

Expected: only scoped source/tests/docs changes; no runtime locks, configurations, databases, state files, or alert ledgers tracked.

- [ ] **Step 4: Replay a read-only market sample locally or on the server**

Aggregate `rejection_counts`, actual rejected `compression_bars`, and eligible rows without writing production state. Confirm rejected rows no longer collapse to 220 bars and explain that zero eligible rows remains possible when all remaining hard gates fail.

- [ ] **Step 5: Request production deployment confirmation**

Report commits, exact verification output, expected behavior change, and remaining strategy tradeoff. Do not upload or restart production until the user explicitly confirms deployment.
