# Compression Price Staleness Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every momentum-compression watch-pool symbol receives redundant prices and a silent WebSocket cannot suppress a valid breakout event.

**Architecture:** Keep the existing WebSocket as the primary source and route both WebSocket and REST prices through `CompressionMonitor._apply_prices()`. Track the last valid WebSocket payload in memory, run one REST ticker check every five seconds while the pool is non-empty, and close/reconnect a stream that has been silent for 15 seconds. Existing state locks and event identity fields remain the only deduplication boundary.

**Tech Stack:** Python 3.12, `threading`, `websocket-client`, `requests`, `unittest`, Flask internal API.

## Global Constraints

- WebSocket staleness threshold is exactly `15_000` milliseconds.
- REST redundancy interval remains exactly 5 seconds and runs only while the observation pool is non-empty.
- A valid miniTicker payload must contain at least one dictionary row with a non-empty symbol and a finite positive `c` price.
- Do not change compression selection rules, observation-pool schema, WeChat configuration, trading behavior, or user configuration.
- Do not add dependencies or user-configurable settings.
- Both price sources must continue through `_apply_prices()` and existing state/event deduplication.
- Do not deploy, restart services, or send test messages as part of this implementation plan.

---

## File Map

- Modify `momentum_compression_monitor.py`: WebSocket heartbeat, staleness detection, redundant REST cycle, status facts.
- Modify `tests/test_momentum_compression_monitor.py`: TDD coverage for heartbeat validity, stale close/reconnect, REST redundancy, TREE incident replay, and duplicate suppression.
- Modify `web_ui.py`: include stream health facts in the loopback-only internal compression status.
- Modify `tests/test_momentum_compression_admin.py`: lock the internal status allowlist.

---

### Task 1: Track Valid WebSocket Price Heartbeats

**Files:**
- Modify: `momentum_compression_monitor.py:43-86,127-176,354-379`
- Test: `tests/test_momentum_compression_monitor.py`

**Interfaces:**
- Consumes: existing `CompressionMonitor.time_ms()`, `handle_message(message, now_ms=None)`, and `status()`.
- Produces: constructor keyword `stream_stale_after_ms: int = 15_000`, runtime field `_last_stream_message_at_ms: int`, and status field `last_price_message_at: int`.

- [ ] **Step 1: Write failing tests for heartbeat creation and validation**

Add these tests to `CompressionMonitorPriceTests`:

```python
def test_open_and_valid_message_update_stream_heartbeat(self):
    with TemporaryDirectory() as folder:
        monitor = self._monitor(Path(folder), time_ms=lambda: 1_000)
        monitor._on_open(object())
        self.assertEqual(monitor.status()["last_price_message_at"], 1_000)

        monitor.handle_message(
            json.dumps([{"s": "OTHERUSDT", "c": "99"}]),
            now_ms=2_000,
        )

    self.assertEqual(monitor.status()["last_price_message_at"], 2_000)

def test_malformed_or_nonfinite_message_does_not_refresh_stream_heartbeat(self):
    with TemporaryDirectory() as folder:
        monitor = self._monitor(Path(folder), time_ms=lambda: 1_000)
        monitor._on_open(object())
        monitor.handle_message(json.dumps([{"s": "POOLUSDT", "c": "bad"}]), now_ms=2_000)

    self.assertEqual(monitor.status()["last_price_message_at"], 1_000)
```

- [ ] **Step 2: Run the new heartbeat tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_open_and_valid_message_update_stream_heartbeat tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_malformed_or_nonfinite_message_does_not_refresh_stream_heartbeat -v
```

Expected: both tests fail because `last_price_message_at` is absent and `_on_open()` does not establish a heartbeat.

- [ ] **Step 3: Implement the minimum heartbeat state**

Extend the constructor and state:

```python
def __init__(
    self,
    state_path: Path,
    snapshot_dir: Path,
    event_callback,
    *,
    websocket_factory=None,
    http_get=requests.get,
    scan=scan_compression_market,
    now=datetime.now,
    time_ms=None,
    sleep=time.sleep,
    thread_factory=threading.Thread,
    stream_stale_after_ms=15_000,
):
    if not isinstance(stream_stale_after_ms, int) or isinstance(stream_stale_after_ms, bool) or stream_stale_after_ms <= 0:
        raise ValueError("stream_stale_after_ms must be a positive integer")
    self.stream_stale_after_ms = stream_stale_after_ms
    self._last_stream_message_at_ms = 0
```

In `_on_open()` set the grace-period heartbeat:

```python
self._last_stream_message_at_ms = self.time_ms()
```

In `handle_message()`, calculate `message_at` once, recognize valid prices independently of pool membership, and refresh the heartbeat only when at least one valid miniTicker row exists:

```python
message_at = self.time_ms() if now_ms is None else now_ms
has_valid_stream_price = False
for row in rows:
    symbol = row.get("s") if isinstance(row, dict) else None
    price = self._finite_price(row.get("c")) if isinstance(row, dict) else None
    if isinstance(symbol, str) and symbol and price is not None:
        has_valid_stream_price = True
        if symbol in symbols:
            prices[symbol] = price
        else:
            self._dropped_price_rows += 1
    else:
        self._dropped_price_rows += 1
if has_valid_stream_price:
    self._last_stream_message_at_ms = message_at
self._apply_prices(prices, message_at)
```

Expose the field from `status()`:

```python
"last_price_message_at": self._last_stream_message_at_ms,
```

- [ ] **Step 4: Run heartbeat tests and existing monitor tests**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor -v
```

Expected: all monitor tests pass; the existing non-pool dropped-row count remains unchanged.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- momentum_compression_monitor.py tests/test_momentum_compression_monitor.py
git commit -m "fix: track compression price heartbeat"
```

---

### Task 2: Add A+ REST Redundancy and Stale Reconnect

**Files:**
- Modify: `momentum_compression_monitor.py:151-220`
- Test: `tests/test_momentum_compression_monitor.py`

**Interfaces:**
- Consumes: Task 1 fields `stream_stale_after_ms` and `_last_stream_message_at_ms`, existing `_pool_symbols()`, `rest_fallback_once()`, `_apply_prices()`, and `_app.close()`.
- Produces: `_close_stale_stream(now_ms: int) -> bool`; returns `True` only for the one check that transitions a connected stream to stale and requests close.

- [ ] **Step 1: Write failing tests for empty-pool skip and connected REST redundancy**

Add a `with_pool=True` option to the test helper so an empty state can be created without changing production APIs, then add:

```python
def test_rest_fallback_skips_request_when_pool_is_empty(self):
    calls = []
    with TemporaryDirectory() as folder:
        monitor = self._monitor(
            Path(folder),
            with_pool=False,
            http_get=lambda *args, **kwargs: calls.append(args),
        )
        result = monitor.rest_fallback_once(now_ms=2_000)

    self.assertTrue(result)
    self.assertEqual(calls, [])

def test_fallback_cycle_uses_rest_even_while_stream_is_connected(self):
    with TemporaryDirectory() as folder:
        monitor = self._monitor(Path(folder), time_ms=lambda: 2_000)
        monitor._stream_connected = True
        calls = []
        monitor.rest_fallback_once = lambda **kwargs: calls.append(kwargs) or True
        monitor.sleep = lambda seconds: monitor._stop.set()
        monitor._fallback_loop()

    self.assertEqual(calls, [{"now_ms": 2_000}])
```

- [ ] **Step 2: Run redundancy tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_rest_fallback_skips_request_when_pool_is_empty tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_fallback_cycle_uses_rest_even_while_stream_is_connected -v
```

Expected: empty-pool test records an HTTP call and connected-cycle test records no REST call.

- [ ] **Step 3: Implement pool-aware REST redundancy**

At the start of `rest_fallback_once()`:

```python
symbols = self._pool_symbols()
if not symbols:
    return True
```

Reuse that `symbols` value when filtering the response. Replace `_fallback_loop()` with one REST cycle per five seconds regardless of connection state:

```python
def _fallback_loop(self):
    while not self._stop.is_set():
        now_ms = self.time_ms()
        self._close_stale_stream(now_ms)
        self.rest_fallback_once(now_ms=now_ms)
        self.sleep(5)
```

- [ ] **Step 4: Write the failing stale-stream close test**

```python
def test_connected_stream_is_closed_once_after_fifteen_seconds_without_prices(self):
    class App:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    with TemporaryDirectory() as folder:
        monitor = self._monitor(Path(folder), time_ms=lambda: 16_000)
        app = App()
        monitor._app = app
        monitor._stream_connected = True
        monitor._price_stream_status = "connected"
        monitor._last_stream_message_at_ms = 1_000

        self.assertTrue(monitor._close_stale_stream(16_000))
        self.assertFalse(monitor._close_stale_stream(16_001))

    self.assertEqual(app.close_calls, 1)
    self.assertFalse(monitor._stream_connected)
    self.assertEqual(monitor.status()["price_stream_status"], "stale")
```

- [ ] **Step 5: Run stale-stream test and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_connected_stream_is_closed_once_after_fifteen_seconds_without_prices -v
```

Expected: FAIL with `AttributeError` because `_close_stale_stream` does not exist.

- [ ] **Step 6: Implement one-shot stale close**

```python
def _close_stale_stream(self, now_ms: int) -> bool:
    with self._lifecycle_lock:
        if (
            not self._stream_connected
            or self._last_stream_message_at_ms <= 0
            or now_ms - self._last_stream_message_at_ms < self.stream_stale_after_ms
        ):
            return False
        self._stream_connected = False
        self._price_stream_status = "stale"
        app = self._app
    if app is not None:
        try:
            app.close()
        except Exception as error:
            self._last_error = f"WebSocket stale close: {error}"
    return True
```

Update `_on_close()` and the post-`run_forever()` transition so they do not overwrite `stale` before the reconnect loop starts. The next loop iteration may set `connecting` normally.

- [ ] **Step 7: Write the TREE incident and cross-source deduplication regression test**

```python
def test_tree_rest_breakout_emits_once_when_websocket_repeats_the_price(self):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"symbol": "TREEUSDT", "price": "0.03972"}]

    with TemporaryDirectory() as folder:
        root = Path(folder)
        state = default_state()
        state["pool"]["tree-long"] = {
            **pool_item("TREEUSDT"),
            "compression_id": "tree-long",
            "upper_boundary_price": 0.03969363636363638,
            "lower_boundary_price": 0.03945749999999999,
            "breakout_buffer_price": 0.000010545513981230876,
            "htf_alignment": "UNKNOWN",
        }
        save_compression_state(root / "state.json", state)
        callbacks = []
        monitor = CompressionMonitor(
            root / "state.json",
            root,
            callbacks.extend,
            http_get=lambda *args, **kwargs: Response(),
            time_ms=lambda: 2_000,
        )
        monitor._stream_connected = True
        monitor._last_stream_message_at_ms = 2_000
        monitor.sleep = lambda seconds: monitor._stop.set()
        monitor._fallback_loop()
        monitor.handle_message(
            json.dumps([{"s": "TREEUSDT", "c": "0.03973"}]),
            now_ms=2_100,
        )

    self.assertEqual(len(callbacks), 1)
    self.assertEqual(callbacks[0]["state"], "BREAKOUT_FRESH_LONG")
    self.assertEqual(callbacks[0]["live_price"], 0.03972)
```

- [ ] **Step 8: Run incident test and verify RED before Task 2 implementation is complete**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor.CompressionMonitorPriceTests.test_tree_rest_breakout_emits_once_when_websocket_repeats_the_price -v
```

Expected: FAIL with `len(callbacks) == 0` because the old fallback loop skips REST while `_stream_connected=True`.

- [ ] **Step 9: Run all monitor tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor -v
```

Expected: all monitor tests pass, including TREE single-event replay, lifecycle generation tests, and shutdown timeout tests.

- [ ] **Step 10: Commit Task 2**

```powershell
git add -- momentum_compression_monitor.py tests/test_momentum_compression_monitor.py
git commit -m "fix: recover stale compression price streams"
```

---

### Task 3: Expose Health Facts and Run Integration Verification

**Files:**
- Modify: `web_ui.py:4549-4563`
- Modify: `tests/test_momentum_compression_admin.py`
- Verify: `tests/test_momentum_compression_integration.py`

**Interfaces:**
- Consumes: Task 1 status keys `price_stream_status` and `last_price_message_at`.
- Produces: loopback `/internal/compression/status` monitor fields with the same names; no public authentication or route changes.

- [ ] **Step 1: Write the failing internal-status allowlist test**

Extend the fake monitor and expected response in `test_loopback_status_returns_only_monitor_and_scan_facts`:

```python
monitor = {
    "running": True,
    "auto_enabled": True,
    "last_scan_at": 101,
    "next_scan_at": "2026-08-22T00:15:00+00:00",
    "structure_scanning": False,
    "scan_started_at": 99,
    "scan_duration_ms": 456,
    "scan_overdue": True,
    "price_stream_status": "stale",
    "last_price_message_at": 88,
    "last_error": "",
    "unexpected": "must not leak",
}
```

Add only `price_stream_status` and `last_price_message_at` to the expected monitor response. Keep `unexpected` excluded.

- [ ] **Step 2: Run the internal API test and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_compression_admin.CompressionInternalApiTests.test_loopback_status_returns_only_monitor_and_scan_facts -v
```

Expected: FAIL because the two health fields are absent from the JSON response.

- [ ] **Step 3: Add health fields to the internal allowlist**

Change only the tuple in `_compression_internal_status()`:

```python
for key in (
    "running", "auto_enabled", "last_scan_at", "next_scan_at",
    "structure_scanning", "scan_started_at", "scan_duration_ms", "scan_overdue",
    "price_stream_status", "last_price_message_at", "last_error",
)
```

- [ ] **Step 4: Run targeted compression tests**

Run:

```powershell
python -m unittest tests.test_momentum_compression_monitor tests.test_momentum_compression_admin tests.test_momentum_compression_integration -v
```

Expected: all targeted tests pass with zero failures and zero errors.

- [ ] **Step 5: Run compilation and complete regression suite**

Run:

```powershell
python -m py_compile momentum_compression_monitor.py web_ui.py
python -m unittest discover -s tests -q
git diff --check
```

Expected: compilation exits 0, the full test suite reports `OK`, and `git diff --check` emits no errors.

- [ ] **Step 6: Review the final diff against scope**

Run:

```powershell
git diff --stat HEAD~2
git diff HEAD~2 -- momentum_compression_monitor.py web_ui.py tests/test_momentum_compression_monitor.py tests/test_momentum_compression_admin.py
```

Verify that no compression thresholds, store schema, WeChat files, trading modules, or configuration files changed.

- [ ] **Step 7: Commit Task 3**

```powershell
git add -- web_ui.py tests/test_momentum_compression_admin.py
git commit -m "fix: expose compression stream freshness"
```

- [ ] **Step 8: Verify the three-commit implementation range**

Run:

```powershell
git diff --stat HEAD~3..HEAD
git status --short
```

Expected: the implementation range contains only the four planned source/test files; worktree status contains no implementation changes.

- [ ] **Step 9: Prepare deployment handoff without deploying**

Report the final commit IDs, exact files requiring deployment (`momentum_compression_monitor.py`, `web_ui.py`), targeted/full test counts, and the production backup/restart plan. Wait for explicit user authorization before uploading or restarting `macd-bot`.
