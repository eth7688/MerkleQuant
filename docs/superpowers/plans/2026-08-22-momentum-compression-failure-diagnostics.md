# Momentum Compression Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show complete compression-scan symbols, retry transient 15-minute K-line failures once, and expose the final failed symbols and causes on the dashboard.

**Architecture:** Preserve the existing shared K-line API by adding an opt-in exception-propagation flag. The compression service owns retry policy and converts final failures into a bounded, validated structure persisted with the latest scan; the status route passes those facts to the existing native-JavaScript dashboard.

**Tech Stack:** Python 3.12, Flask, requests, pandas, SQLite-independent JSON state, native HTML/CSS/JavaScript, unittest, Node.js test harness.

## Global Constraints

- Do not change compression selection rules, the 1,000,000 USDT volume floor, pool state transitions, breakout rules, sound alerts, WeChat delivery, or trading logic.
- Existing `fetch_klines()` callers must keep their current return-`None` failure behavior unless they explicitly opt into exception propagation.
- Persist only the most recent scan's final failures; never accumulate an unbounded history.
- Every displayed backend string must pass through `escapeRHtml()`.
- Make local changes before any server deployment. Deployment is outside this plan until separately authorized.

---

### Task 1: Opt-in K-line exception propagation

**Files:**
- Modify: `screener.py:203-236`
- Test: `tests/test_binance_kline_routing.py`

**Interfaces:**
- Consumes: existing `fetch_klines(symbol, interval, limit=200, exchange=None, closed_only=True, market_type=None, testnet=None, price_type=None, bitget_granularity=None) -> pandas.DataFrame | None`
- Produces: the same interface with a final `raise_errors: bool = False` parameter; when `raise_errors=True`, request/parsing exceptions are re-raised.

- [ ] **Step 1: Write failing compatibility and propagation tests**

Add these tests to `BinanceKlineRoutingTest`:

```python
    def test_fetch_klines_keeps_legacy_none_on_request_failure(self):
        with patch("screener.requests.get", side_effect=TimeoutError("slow")):
            self.assertIsNone(fetch_klines("DOTUSDT", "15m", 2))

    def test_fetch_klines_can_propagate_request_failure(self):
        with patch("screener.requests.get", side_effect=TimeoutError("slow")):
            with self.assertRaisesRegex(TimeoutError, "slow"):
                fetch_klines("DOTUSDT", "15m", 2, raise_errors=True)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_binance_kline_routing.BinanceKlineRoutingTest.test_fetch_klines_keeps_legacy_none_on_request_failure tests.test_binance_kline_routing.BinanceKlineRoutingTest.test_fetch_klines_can_propagate_request_failure -v
```

Expected: the legacy test passes and the propagation test fails because `fetch_klines()` does not accept `raise_errors`.

- [ ] **Step 3: Add the opt-in parameter without changing defaults**

Change the signature in `screener.py` to:

```python
def fetch_klines(symbol, interval, limit=200, exchange=None, closed_only=True,
                 market_type=None, testnet=None, price_type=None,
                 bitget_granularity=None, raise_errors=False):
```

Keep the function body unchanged except for replacing its final bare handler with:

```python
    except Exception:
        if raise_errors:
            raise
        return None
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command again.

Expected: both tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- screener.py tests/test_binance_kline_routing.py
git commit -m "fix: expose compression kline request errors"
```

---

### Task 2: Retry once and persist bounded failure facts

**Files:**
- Modify: `momentum_compression_service.py:131-240`
- Modify: `momentum_compression_store.py:36-49,144-178,257-271`
- Test: `tests/test_momentum_compression_service.py`
- Test: `tests/test_momentum_compression_store.py`

**Interfaces:**
- Consumes: `fetch_klines()` with `raise_errors=True` from Task 1.
- Produces: `CompressionSymbolScanError`, `_failure_detail(symbol, error, *, stage, attempts) -> dict`, state key `last_scan_failures: list[dict]`, and report key `failed_details: list[dict]`.
- Failure item schema: `{"symbol": str, "stage": str, "error_type": str, "message": str, "attempts": int}`.

- [ ] **Step 1: Write failing retry tests**

Add to `CompressionScanFailureIsolationTests`:

```python
    def test_symbol_kline_failure_retries_once_then_succeeds_without_error(self):
        from momentum_compression_service import scan_compression_market

        frame = trend_frame("LONG", 220)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("momentum_compression_service.fetch_compression_universe", return_value=(["KEEPUSDT"], {})), \
                 patch("momentum_compression_service.fetch_live_prices", return_value={"KEEPUSDT": 105.0}), \
                 patch("momentum_compression_service.fetch_klines", side_effect=[TimeoutError("slow"), frame]) as fetch, \
                 patch("momentum_compression_service.evaluate_both_sides", return_value=[]), \
                 patch("momentum_compression_service.time.sleep"):
                report = scan_compression_market(root / "state.json", root, max_workers=1)

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["failed_details"], [])

    def test_symbol_kline_failure_after_retry_records_exact_reason(self):
        from momentum_compression_service import scan_compression_market

        with TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("momentum_compression_service.fetch_compression_universe", return_value=(["KEEPUSDT"], {})), \
                 patch("momentum_compression_service.fetch_live_prices", return_value={"KEEPUSDT": 105.0}), \
                 patch("momentum_compression_service.fetch_klines", side_effect=TimeoutError("  upstream\nslow  ")), \
                 patch("momentum_compression_service.evaluate_both_sides", return_value=[]), \
                 patch("momentum_compression_service.time.sleep"):
                report = scan_compression_market(root / "state.json", root, max_workers=1)
            stored = load_compression_state(root / "state.json")

        expected = {
            "symbol": "KEEPUSDT", "stage": "15m_klines",
            "error_type": "TimeoutError", "message": "upstream slow", "attempts": 2,
        }
        self.assertEqual(report["failed_details"], [expected])
        self.assertEqual(report["errors"], 1)
        self.assertEqual(stored["last_scan_failures"], [expected])
```

- [ ] **Step 2: Write failing price-missing and latest-scan state tests**

Extend `CompressionScanFailureIsolationTests.test_missing_bulk_price_preserves_pool_entry_as_unavailable` with:

```python
        self.assertEqual(report["failed_details"], [{
            "symbol": "AUSDT", "stage": "live_price",
            "error_type": "MissingPrice", "message": "live price unavailable", "attempts": 0,
        }])
```

Add to `CompressionStatePersistenceTests` in `tests/test_momentum_compression_store.py`:

```python
    def test_current_version_without_failure_field_migrates_to_empty_list(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            legacy = default_state()
            legacy.pop("last_scan_failures", None)
            path.write_text(json.dumps(legacy), encoding="utf-8")

            loaded = load_compression_state(path)

        self.assertEqual(loaded["last_scan_failures"], [])

    def test_failure_details_reject_unbounded_or_invalid_payloads(self):
        state = default_state()
        state["last_scan_failures"] = [{
            "symbol": "KEEPUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
            "message": "x" * 161, "attempts": 2,
        }]
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "scan failure"):
                save_compression_state(Path(folder) / "state.json", state)
```

- [ ] **Step 3: Run the new service and store tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression_service.CompressionScanFailureIsolationTests tests.test_momentum_compression_store -v
```

Expected: failures mention the missing retry, `failed_details`, and `last_scan_failures` behavior.

- [ ] **Step 4: Implement the bounded failure model and retry loop**

Add to `momentum_compression_service.py`:

```python
class CompressionSymbolScanError(RuntimeError):
    def __init__(self, detail):
        super().__init__(detail["message"])
        self.detail = detail


def _failure_detail(symbol, error, *, stage, attempts):
    message = " ".join(str(error).split())[:160] or error.__class__.__name__
    return {
        "symbol": symbol,
        "stage": stage,
        "error_type": error.__class__.__name__,
        "message": message,
        "attempts": attempts,
    }
```

Replace the single 15-minute fetch in `_scan_symbol()` with:

```python
    frame = None
    for attempt in (1, 2):
        try:
            frame = fetch_klines(
                symbol, "15m", 220, exchange="binance", closed_only=True,
                market_type="futures", testnet=False, raise_errors=True,
            )
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise ValueError("15m candle history is unavailable")
            break
        except Exception as error:
            if attempt == 2:
                raise CompressionSymbolScanError(
                    _failure_detail(symbol, error, stage="15m_klines", attempts=2)
                ) from error
            time.sleep(0.2)
```

In `scan_compression_market()`, replace the scalar error bookkeeping with:

```python
    failed_details = [
        _failure_detail(
            symbol, ValueError("live price unavailable"),
            stage="live_price", attempts=0,
        ) | {"error_type": "MissingPrice"}
        for symbol in symbols if symbol not in live_prices
    ]
```

At the future boundary, retain custom details and normalize unexpected worker failures:

```python
            try:
                rows, symbol_frames, close_ms = future.result()
            except CompressionSymbolScanError as error:
                failed_details.append(error.detail)
            except Exception as error:
                failed_details.append(
                    _failure_detail(symbol, error, stage="symbol_scan", attempts=1)
                )
            else:
                evaluations.extend(rows)
                frames.update(symbol_frames)
                close_times.append(close_ms)
```

Before reconciliation, derive the compatibility list and persist the latest details:

```python
    failed_symbols = [item["symbol"] for item in failed_details]
    errors = len(failed_details)
    # Inside compression_state_lock:
    state["last_scan_failures"] = failed_details
    state["last_error"] = f"{errors} symbol scan failures" if errors else ""
```

Return both `"failed_symbols": failed_symbols` and `"failed_details": failed_details` in the report.

Add to `default_state()`:

```python
        "last_scan_failures": [],
```

Add this validator and call it from `_validate_state()`:

```python
def _validate_scan_failures(failures):
    if not isinstance(failures, list) or len(failures) > 1000:
        raise ValueError("invalid scan failures")
    required = {"symbol", "stage", "error_type", "message", "attempts"}
    for item in failures:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("invalid scan failure")
        for key in ("symbol", "stage", "error_type", "message"):
            if not isinstance(item[key], str) or not item[key]:
                raise ValueError("invalid scan failure")
        if len(item["message"]) > 160 or not _is_int(item["attempts"]) or item["attempts"] < 0:
            raise ValueError("invalid scan failure")


# Inside _validate_state(state):
    _validate_scan_failures(state["last_scan_failures"])
```

Update the current-version compatibility branch in `load_compression_state()` to fill either missing optional field:

```python
    elif state.get("version") == STATE_VERSION:
        missing = set(default_state()) - set(state)
        if missing <= {"legacy_unpublished_event_ids", "last_scan_failures"} and not (set(state) - set(default_state())):
            state = dict(state)
            state.setdefault("legacy_unpublished_event_ids", [])
            state.setdefault("last_scan_failures", [])
```

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

Run the Step 3 command again.

Expected: all selected tests pass; retry test performs exactly two fetch calls and no real sleep.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- momentum_compression_service.py momentum_compression_store.py tests/test_momentum_compression_service.py tests/test_momentum_compression_store.py
git commit -m "fix: retain compression scan failure details"
```

---

### Task 3: Expose failure details and show full symbols

**Files:**
- Modify: `web_ui.py:4479-4523,1929-1994`
- Test: `tests/test_momentum_compression_integration.py`

**Interfaces:**
- Consumes: state key `last_scan_failures` from Task 2.
- Produces: `/api/compression/status` response key `scan_failures: list[dict]`; dashboard failure-details section and complete symbol text.

- [ ] **Step 1: Write failing API and renderer tests**

In `CompressionApiTests.test_status_maps_alert_and_delivery_facts_to_pool_and_terminal_rows`, include this field in the mocked state returned by `load_compression_state`:

```python
state["last_scan_failures"] = [{
    "symbol": "BADUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
    "message": "upstream <slow>", "attempts": 2,
}]
```

Then assert:

```python
self.assertEqual(payload["scan_failures"], [{
    "symbol": "BADUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
    "message": "upstream <slow>", "attempts": 2,
}])
```

Extend `test_renderer_escapes_payload_and_formats_nonfinite_values` with:

```python
            "scan_failures": [{
                "symbol": "BADUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
                "message": "upstream <slow>", "attempts": 2,
            }],
```

and assertions:

```python
        self.assertIn("FRESHUSDT", rendered)
        self.assertIn("扫描失败明细", rendered)
        self.assertIn("BADUSDT", rendered)
        self.assertIn("upstream &lt;slow&gt;", rendered)
        self.assertNotIn("upstream <slow>", rendered)
```

- [ ] **Step 2: Run focused integration tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression_integration.CompressionDashboardUiTests.test_renderer_escapes_payload_and_formats_nonfinite_values -v
```

Also run the exact compression status API test discovered with:

```powershell
python -m unittest tests.test_momentum_compression_integration -v
```

Expected: assertions for complete symbols, `scan_failures`, and the details section fail.

- [ ] **Step 3: Pass persisted failures through the status route**

Add to the JSON object returned by `compression_status()`:

```python
        "scan_failures": list(compression_state.get("last_scan_failures", [])),
```

- [ ] **Step 4: Render complete symbols and escaped failure details**

In `compressionRowHtml()` and the current/terminal breakout table mappings, replace:

```javascript
escapeRHtml(row.symbol||'--').replace('USDT','')
```

with:

```javascript
escapeRHtml(row.symbol||'--')
```

Inside `renderMomentumCompression()`, read and render the structured list:

```javascript
  var failures=Array.isArray(payload.scan_failures)?payload.scan_failures:[];
  var failureHtml=failures.length?'<section class="reflow-dashboard" style="margin-top:12px"><div style="padding:12px 14px;border-bottom:1px solid var(--border);font-size:12px;font-weight:700">扫描失败明细 <span class="r">'+failures.length+'</span></div><table><thead><tr><th>交易对</th><th>阶段</th><th>错误类型</th><th>原因</th><th>尝试次数</th></tr></thead><tbody>'+failures.map(function(item){item=item&&typeof item==='object'?item:{};return '<tr><td><b>'+escapeRHtml(item.symbol||'--')+'</b></td><td>'+escapeRHtml(item.stage||'--')+'</td><td>'+escapeRHtml(item.error_type||'--')+'</td><td>'+escapeRHtml(item.message||'--')+'</td><td>'+compressionNumber(item.attempts,0)+'</td></tr>';}).join('')+'</tbody></table></section>':'';
```

Insert `failureHtml` after the controls and before the LONG/SHORT pool tables.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_momentum_compression_integration -v
```

Expected: all integration tests pass and the Node harness contains escaped failure text and full `USDT` symbols.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- web_ui.py tests/test_momentum_compression_integration.py
git commit -m "fix: show compression scan failures"
```

---

### Task 4: Regression verification and handoff

**Files:**
- Verify only: `screener.py`, `momentum_compression_service.py`, `momentum_compression_store.py`, `web_ui.py`, related tests

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: verified local branch ready for a separately authorized production deployment.

- [ ] **Step 1: Run focused regression tests**

```powershell
python -m unittest tests.test_binance_kline_routing tests.test_momentum_compression_service tests.test_momentum_compression_store tests.test_momentum_compression_integration -v
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the full test suite**

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass with no new errors.

- [ ] **Step 3: Compile changed Python modules**

```powershell
python -m py_compile screener.py momentum_compression_service.py momentum_compression_store.py web_ui.py
```

Expected: exit code 0 and no output.

- [ ] **Step 4: Inspect the final diff and repository status**

```powershell
git diff HEAD~3 --check
git diff HEAD~3 --stat
git status --short
```

Expected: no whitespace errors; only the planned source/test files plus the two pre-existing runtime lock files are present.

- [ ] **Step 5: Report local completion without deploying**

Report the exact test counts and changed files. State explicitly that production was not changed and request separate deployment authorization.
