# Momentum Reflow Auto Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the 1H momentum-reflow scanner into a global hourly auto-scanner with an administrator toggle, non-equity Binance futures coverage, transparent quality scoring, and a persistent Beijing-day signal dashboard.

**Architecture:** Keep market detection in `momentum_reflow.py`, move settings/scoring/history into a focused `momentum_reflow_dashboard.py`, and let `web_ui.py` own the single in-process scheduler because it already owns the shared scan worker lock. `admin_server.py` writes the atomic global setting and reads scheduler status without touching trading configuration.

**Tech Stack:** Python 3.12, Flask, pandas, requests, native HTML/CSS/JavaScript, JSON atomic persistence, `unittest`, Node-based DOM smoke tests already used by the project.

## Global Constraints

- Data source is Binance mainnet USDT futures; use only closed 1H and daily candles.
- `REFLOW_MIN_VOLUME_USDT` is exactly `2_000_000`; do not change shared `screener.MIN_VOLUME`.
- Admit ordinary non-equity `PERPETUAL` contracts plus `TRADIFI_PERPETUAL` only when `underlyingType` is `COMMODITY` or `FX`.
- Reject `EQUITY`, `HK_EQUITY`, `KR_EQUITY`, `PREMARKET`, stablecoin pairs, and names containing `BULL`, `BEAR`, `UP`, or `DOWN`.
- Auto scan defaults on, runs once after service start/enabling, then at Beijing time `HH:03`.
- Auto, manual, funding, and generic scans share one global worker slot; no timeout-based forced unlock and no overlapping workers.
- Today means Beijing time `00:00:00` through the current time.
- Persist an observed signal for the full Beijing day even after its event becomes terminal.
- Primary ordering is newest `return_open_time`; quality score is only the secondary key.
- Quality score is transparent and must not be described as win probability.
- Only administrators can change the global switch; ordinary users can view status only.
- Do not modify Predicta, RJ, order placement, risk, positions, stops, exits, PnL, authentication rules, API keys, or trading configuration.
- No external frontend framework or npm dependency.
- All runtime JSON writes use temp file, flush, `fsync`, and atomic replace. Corrupt input fails closed and is never overwritten.
- Do not deploy or restart production services until the user gives explicit deployment approval after implementation review.

---

### Task 1: Expand the Reflow Universe and Attach Instrument Types

**Files:**
- Modify: `momentum_reflow.py:15-21`
- Modify: `momentum_reflow.py:377-400`
- Modify: `momentum_reflow.py:513-567`
- Modify: `tests/test_momentum_reflow.py:465-534`

**Interfaces:**
- Produces: `REFLOW_MIN_VOLUME_USDT: int`
- Produces: `classify_futures_contract(row: dict) -> str | None`
- Changes: `fetch_futures_universe() -> tuple[list[str], dict[str, float], dict[str, str]]`
- Extends scan rows with `instrument_type` equal to `CRYPTO`, `COMMODITY`, or `FX`.

- [ ] **Step 1: Write failing universe-classification tests**

Add focused cases to `ReflowScanServiceTests`:

```python
def test_reflow_universe_uses_two_million_and_allows_non_equity_tradifi(self):
    exchange_rows = [
        {"symbol": "BTCUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL",
         "status": "TRADING", "underlyingType": "COIN"},
        {"symbol": "XAUUSDT", "quoteAsset": "USDT", "contractType": "TRADIFI_PERPETUAL",
         "status": "TRADING", "underlyingType": "COMMODITY"},
        {"symbol": "EURUSDT", "quoteAsset": "USDT", "contractType": "TRADIFI_PERPETUAL",
         "status": "TRADING", "underlyingType": "FX"},
        {"symbol": "TSLAUSDT", "quoteAsset": "USDT", "contractType": "TRADIFI_PERPETUAL",
         "status": "TRADING", "underlyingType": "EQUITY"},
        {"symbol": "OPENAIUSDT", "quoteAsset": "USDT", "contractType": "TRADIFI_PERPETUAL",
         "status": "TRADING", "underlyingType": "PREMARKET"},
        {"symbol": "USDCUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL",
         "status": "TRADING", "underlyingType": "COIN"},
    ]
    tickers = [
        {"symbol": row["symbol"], "quoteVolume": "2000000"}
        for row in exchange_rows
    ]
    with patch("momentum_reflow.requests.get") as get:
        get.side_effect = [
            MockResponse({"symbols": exchange_rows}),
            MockResponse(tickers),
        ]
        symbols, _, types = fetch_futures_universe()
    self.assertEqual(symbols, ["BTCUSDT", "XAUUSDT", "EURUSDT"])
    self.assertEqual(types, {
        "BTCUSDT": "CRYPTO",
        "XAUUSDT": "COMMODITY",
        "EURUSDT": "FX",
    })

def test_reflow_volume_boundary_is_inclusive(self):
    # Build one valid BTC row and ticker quoteVolume exactly 2_000_000.
    symbols, _, _ = fetch_futures_universe()
    self.assertEqual(symbols, ["BTCUSDT"])
```

Add rejection cases for `HK_EQUITY`, `KR_EQUITY`, unknown `TRADIFI_PERPETUAL`, and symbols containing `BULL`, `BEAR`, `UP`, or `DOWN`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow.ReflowScanServiceTests -v
```

Expected: FAIL because the current function returns two values, requires `contractType=PERPETUAL`, and still uses shared `MIN_VOLUME=3_000_000`.

- [ ] **Step 3: Implement the dedicated universe policy**

In `momentum_reflow.py`, remove the `MIN_VOLUME` import and add:

```python
REFLOW_MIN_VOLUME_USDT = 2_000_000
STABLE_BASE_ASSETS = {
    "USDC", "FDUSD", "USD1", "RLUSD", "TUSD", "DAI", "USDP", "USDD",
    "PYUSD", "USDY", "CRVUSD", "SUSD", "EUSD", "GHO", "LUSD", "MIM",
    "FRAX", "USTC", "USDE", "USR", "EURS", "EURC", "XSGD", "USDJ",
    "USDX", "USDB", "USDZ", "AEUR", "USDF", "STUSD", "USDQ", "XUSD",
    "USDS",
}
LEVERAGED_MARKERS = ("BULL", "BEAR", "UP", "DOWN")
ALLOWED_TRADIFI_TYPES = {"COMMODITY": "COMMODITY", "FX": "FX"}
BLOCKED_EQUITY_TYPES = {"EQUITY", "HK_EQUITY", "KR_EQUITY", "PREMARKET"}

def classify_futures_contract(row: dict) -> str | None:
    symbol = str(row.get("symbol", "")).upper()
    base = str(row.get("baseAsset") or symbol.removesuffix("USDT")).upper()
    contract_type = row.get("contractType")
    underlying_type = str(row.get("underlyingType", "")).upper()
    if row.get("quoteAsset") != "USDT" or row.get("status") != "TRADING":
        return None
    if base in STABLE_BASE_ASSETS or any(marker in base for marker in LEVERAGED_MARKERS):
        return None
    if underlying_type in BLOCKED_EQUITY_TYPES:
        return None
    if contract_type == "PERPETUAL":
        return "CRYPTO"
    if contract_type == "TRADIFI_PERPETUAL":
        return ALLOWED_TRADIFI_TYPES.get(underlying_type)
    return None
```

Build the type map for every admitted contract, then select eligible symbols using `quoteVolume >= REFLOW_MIN_VOLUME_USDT`. Return `(symbols, volume, instrument_types)`.

Update `scan_momentum_reflow()`:

```python
eligible_symbols, _, instrument_types = fetch_futures_universe()
symbols = sorted(set(eligible_symbols) | _active_ledger_symbols(ledger))
# After a worker returns a candidate:
candidate["instrument_type"] = instrument_types.get(symbol, "CRYPTO")
```

Do not drop an active-ledger symbol merely because it is below the current volume threshold.

- [ ] **Step 4: Run focused and existing scanner tests**

Run:

```powershell
python -m unittest tests.test_momentum_reflow.ReflowScanServiceTests -v
python -m unittest tests.test_momentum_reflow -v
```

Expected: all tests pass, including active-ledger retention and exact 2,000,000 boundary.

- [ ] **Step 5: Commit**

```powershell
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: expand momentum reflow universe"
```

---

### Task 2: Add Atomic Settings, Quality Scoring, and Daily Signal History

**Files:**
- Create: `momentum_reflow_dashboard.py`
- Create: `tests/test_momentum_reflow_dashboard.py`

**Interfaces:**
- Produces: `DEFAULT_REFLOW_SETTINGS: dict`
- Produces: `load_reflow_settings(path: Path) -> dict`
- Produces: `save_reflow_settings(path: Path, enabled: bool, updated_by: str, now_ms: int) -> dict`
- Produces: `score_reflow_candidate(candidate: dict) -> dict`
- Produces: `merge_reflow_signals(history_path: Path, ledger_path: Path, scan_result: dict, now_ms: int) -> dict`
- Produces: `load_reflow_dashboard(history_path: Path, now_ms: int) -> dict`
- Produces: `next_reflow_scan_at(now: datetime) -> datetime`

- [ ] **Step 1: Write failing settings and atomic-file tests**

Create `tests/test_momentum_reflow_dashboard.py` with:

```python
class ReflowSettingsTests(unittest.TestCase):
    def test_missing_settings_default_to_enabled(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = load_reflow_settings(path)
            self.assertTrue(settings["auto_scan_enabled"])
            self.assertEqual(settings["version"], 1)
            self.assertTrue(path.exists())

    def test_corrupt_settings_fail_closed_without_overwrite(self):
        path.write_text("{broken", encoding="utf-8")
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            load_reflow_settings(path)
        self.assertEqual(path.read_bytes(), original)

    def test_save_requires_real_boolean(self):
        with self.assertRaises(TypeError):
            save_reflow_settings(path, 1, "admin", 123)
```

Mock `os.replace` and assert a failed atomic replace preserves the old file.

- [ ] **Step 2: Run settings tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_dashboard.ReflowSettingsTests -v
```

Expected: ERROR with `ModuleNotFoundError: momentum_reflow_dashboard`.

- [ ] **Step 3: Implement versioned atomic settings**

In `momentum_reflow_dashboard.py` implement one private atomic writer:

```python
SETTINGS_VERSION = 1
HISTORY_VERSION = 1
DEFAULT_REFLOW_SETTINGS = {
    "version": SETTINGS_VERSION,
    "auto_scan_enabled": True,
    "updated_at": 0,
    "updated_by": "",
}

def _atomic_write_json(path: Path, payload: dict) -> None:
    # mkdir, mkstemp in the target directory, json.dump, flush, fsync,
    # os.replace, and unlink the temp file on failure.
```

`load_reflow_settings()` must create the default only when the file does not exist. Existing invalid JSON, non-object JSON, wrong version, or non-boolean `auto_scan_enabled` raises `ValueError` without rewriting the file.

- [ ] **Step 4: Write failing quality-score boundary tests**

Add:

```python
class ReflowQualityScoreTests(unittest.TestCase):
    def test_high_quality_score_uses_confirmed_caps(self):
        row = {
            "daily_rank": 3,
            "breakout_volume_ratio": 3.5,
            "max_expansion_atr": 4.0,
            "close_distance_atr": 0.0,
            "window_index": 1,
        }
        scored = score_reflow_candidate(row)
        self.assertEqual(scored["quality_score"], 100)
        self.assertEqual(scored["quality_label"], "HIGH")

    def test_score_boundaries_are_stable(self):
        def row_for(total, daily_rank, window_index):
            daily_points = {3: 30, 2: 24, 1: 18}
            window_points = {1: 10, 2: 8, 3: 6, 4: 4, 5: 2}
            fixed = daily_points[daily_rank] + 15 + 12 + window_points[window_index]
            distance_points = total - fixed
            return {
                "daily_rank": daily_rank,
                "breakout_volume_ratio": 1.5,
                "max_expansion_atr": 1.5,
                "close_distance_atr": (15 - distance_points) * 0.35 / 15,
                "window_index": window_index,
            }

        cases = [
            (75, 2, 1, "HIGH"),
            (74, 2, 1, "STANDARD"),
            (60, 1, 5, "STANDARD"),
            (59, 1, 5, "WATCH"),
        ]
        for total, daily_rank, window_index, label in cases:
            scored = score_reflow_candidate(
                row_for(total, daily_rank, window_index)
            )
            self.assertEqual(scored["quality_score"], total)
            self.assertEqual(scored["quality_label"], label)
```

Also assert the five component values for one boundary row so a compensating arithmetic defect cannot leave only the final total passing.

- [ ] **Step 5: Run score tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_dashboard.ReflowQualityScoreTests -v
```

Expected: FAIL because `score_reflow_candidate` does not exist.

- [ ] **Step 6: Implement the exact transparent score**

Implement:

```python
DAILY_POINTS = {3: 30.0, 2: 24.0, 1: 18.0}
WINDOW_POINTS = {1: 10.0, 2: 8.0, 3: 6.0, 4: 4.0, 5: 2.0}

def _linear(value, start, end, start_points, end_points):
    value = min(max(float(value), start), end)
    ratio = (value - start) / (end - start)
    return start_points + ratio * (end_points - start_points)

def score_reflow_candidate(candidate: dict) -> dict:
    daily = DAILY_POINTS[int(candidate["daily_rank"])]
    volume = _linear(candidate["breakout_volume_ratio"], 1.5, 3.0, 15.0, 25.0)
    expansion = _linear(candidate["max_expansion_atr"], 1.5, 3.0, 12.0, 20.0)
    distance = 15.0 * (1.0 - min(abs(float(candidate["close_distance_atr"])), 0.35) / 0.35)
    window = WINDOW_POINTS[int(candidate["window_index"])]
    total = int(round(daily + volume + expansion + distance + window))
    label = "HIGH" if total >= 75 else "STANDARD" if total >= 60 else "WATCH"
    return {
        **candidate,
        "quality_score": min(100, max(0, total)),
        "quality_label": label,
        "score_components": {
            "daily": round(daily, 4),
            "volume": round(volume, 4),
            "expansion": round(expansion, 4),
            "distance": round(distance, 4),
            "window": round(window, 4),
        },
    }
```

Reject missing, invalid, or non-finite required fields with `ValueError`; do not invent a score.

- [ ] **Step 7: Write failing daily-history tests**

Cover:

```python
class ReflowDailyHistoryTests(unittest.TestCase):
    def test_same_signal_upserts_and_newer_signal_sorts_first(self):
        first = candidate(return_open_time=1000, window_index=1)
        update = candidate(return_open_time=1000, window_index=2)
        newer = candidate(return_open_time=2000, window_index=1)
        merge_reflow_signals(history, ledger, {"rows": [first]}, now_ms)
        merge_reflow_signals(history, ledger, {"rows": [update, newer]}, now_ms + 1)
        rows = load_reflow_dashboard(history, now_ms + 1)["rows"]
        self.assertEqual([r["return_open_time"] for r in rows], [2000, 1000])
        self.assertEqual(rows[1]["window_index"], 2)

    def test_beijing_midnight_separates_days(self):
        before = milliseconds("2026-07-30T15:59:59Z")
        after = milliseconds("2026-07-30T16:00:00Z")
        self.assertNotEqual(beijing_day(before), beijing_day(after))

    def test_terminal_ledger_updates_status_without_deleting_signal(self):
        # Record candidate, then write matching CONSUMED event with
        # audit_reason=return_window_complete and merge an empty scan.
        self.assertEqual(row["status"], "WINDOW_COMPLETE")
```

Also cover `INVALID`, corrupt history preservation, stable ordering ties, history from previous days remaining on disk but absent from today's `rows`, and HTML-independent freshness fields (`return_close_time`, `first_seen_at`, `last_seen_at`).

- [ ] **Step 8: Run history tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_dashboard.ReflowDailyHistoryTests -v
```

Expected: FAIL because daily-history functions are not implemented.

- [ ] **Step 9: Implement daily history and scheduler time helper**

Use this root shape:

```json
{
  "version": 1,
  "days": {
    "2026-07-30": {
      "signals": {}
    }
  }
}
```

Signal key:

```python
def reflow_signal_key(row):
    return "|".join([
        row["symbol"],
        row["direction"],
        str(int(row["breakout_time"])),
        str(int(row["return_open_time"])),
    ])
```

Derive `return_close_time = return_open_time + 3_600_000`. Map current ledger state:

- matching `RETURN_WINDOW` -> `ACTIVE`
- matching `CONSUMED` with `audit_reason == "return_window_complete"` -> `WINDOW_COMPLETE`
- every other terminal matching event -> `INVALID`

Sort today rows by:

```python
key=lambda row: (
    -int(row["return_open_time"]),
    -int(row["quality_score"]),
    -float(row["breakout_volume_ratio"]),
    row["symbol"],
)
```

Implement `next_reflow_scan_at(now)` with timezone-aware datetimes. For Beijing `10:02`, return `10:03`; for `10:03` or later, return `11:03`.

- [ ] **Step 10: Run the new module tests**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_dashboard -v
python -m py_compile momentum_reflow_dashboard.py
```

Expected: all tests pass.

- [ ] **Step 11: Commit**

```powershell
git add momentum_reflow_dashboard.py tests/test_momentum_reflow_dashboard.py
git commit -m "feat: persist and score reflow signals"
```

---

### Task 3: Integrate the Shared Scan Pipeline and Hourly Scheduler

**Files:**
- Modify: `web_ui.py:18`
- Modify: `web_ui.py:421-431`
- Modify: `web_ui.py:3589-3681`
- Modify: `web_ui.py:4110-4114` (the `__main__` startup block; use the actual final line numbers)
- Modify: `tests/test_momentum_reflow_integration.py`
- Create: `tests/test_momentum_reflow_scheduler.py`

**Interfaces:**
- Consumes all Task 2 interfaces.
- Produces: `_run_reflow_scan(progress) -> dict`
- Produces: `_reflow_dashboard_payload(base: dict | None = None, now_ms: int | None = None) -> dict`
- Produces: `_reflow_scheduler_step(now: datetime, settings: dict, state: dict, start_scan: Callable[[], bool]) -> dict`
- Produces: `_start_reflow_scheduler() -> bool`
- Produces: `_stop_reflow_scheduler_for_tests() -> None`
- Produces: `GET /api/reflow/automation/status`
- Extends `/data` reflow payload with scheduler status and today's persistent rows.

- [ ] **Step 1: Write failing shared-pipeline tests**

Add integration tests proving:

```python
def test_manual_reflow_scan_merges_today_history(self):
    scan_result = {"rows": [candidate()], "scanned": 283, "errors": 0, "initialized": 0}
    with patch.object(web_ui, "scan_momentum_reflow", return_value=scan_result), \
         patch.object(web_ui, "merge_reflow_signals", return_value=dashboard_payload):
        response = client.get("/scan/reflow/1h")
        wait_for_payload("reflow_1h", dashboard_payload)
    self.assertEqual(response.status_code, 200)
    self.assertEqual(web_ui.cache["reflow_1h"], dashboard_payload)

def test_other_scan_running_blocks_auto_reflow(self):
    blocker = Event()
    # Start a generic worker whose work waits on blocker.
    self.assertFalse(web_ui._start_automatic_reflow_scan())
    blocker.set()
```

Assert no second `scan_momentum_reflow` call occurs.

- [ ] **Step 2: Run integration tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_integration -v
```

Expected: FAIL because the route currently caches only the instantaneous scan result and no automatic entry point exists.

- [ ] **Step 3: Implement one shared reflow work function**

Add paths beside `MOMENTUM_REFLOW_LEDGER`:

```python
MOMENTUM_REFLOW_SETTINGS = Path(_BASE_DIR) / "momentum_reflow_settings.json"
MOMENTUM_REFLOW_HISTORY = Path(_BASE_DIR) / "momentum_reflow_daily_signals.json"
```

Implement:

```python
def _run_reflow_scan(progress):
    result = scan_momentum_reflow(MOMENTUM_REFLOW_LEDGER, progress=progress)
    return merge_reflow_signals(
        MOMENTUM_REFLOW_HISTORY,
        MOMENTUM_REFLOW_LEDGER,
        result,
        int(time.time() * 1000),
    )

def _start_reflow_scan(trigger):
    label = "动能回流自动扫描" if trigger == "auto" else "reflow 1h 扫描中"
    def apply_result(payload):
        cache["reflow_1h"] = payload
        _update_reflow_automation_success(trigger, payload)
        return len(payload["rows"])
    return _start_scan_worker(label, _run_reflow_scan, apply_result)
```

`_reflow_dashboard_payload()` must call `load_reflow_dashboard()` when `base` is absent, otherwise copy `base`, then overlay a locked snapshot of `_reflow_automation`. The manual route must call `_start_reflow_scan("manual")`. Every `/data` response must use `_reflow_dashboard_payload(cache.get("reflow_1h"))` so switch, progress, next-run, and error status are current even between scans. Loading the home page or `/data` must load today's persisted dashboard when cache is empty, without initiating a market request.

Keep the existing worker-token/identity guard on progress, result, error, and cleanup writes. Add an integration assertion that a stale worker callback cannot overwrite `cache["reflow_1h"]` or the automation status after a newer worker owns the slot.

- [ ] **Step 4: Write failing deterministic scheduler tests**

In `tests/test_momentum_reflow_scheduler.py`, test the pure scheduler step directly rather than sleeping:

```python
def test_enabled_scheduler_runs_immediately_then_at_next_hh03(self):
    starts = []
    state = _new_reflow_scheduler_state()
    state = web_ui._reflow_scheduler_step(
        datetime(2026, 7, 30, 10, 1, tzinfo=BEIJING),
        {"auto_scan_enabled": True},
        state,
        lambda: starts.append("scan") or True,
    )
    self.assertEqual(starts, ["scan"])
    self.assertEqual(
        state["next_scan_at"],
        milliseconds("2026-07-30T02:03:00Z"),
    )

def test_disabled_scheduler_makes_no_scan_request(self):
    state = _new_reflow_scheduler_state()
    starts = []
    for minute in (1, 2, 3):
        state = web_ui._reflow_scheduler_step(
            datetime(2026, 7, 30, 10, minute, tzinfo=BEIJING),
            {"auto_scan_enabled": False},
            state,
            lambda: starts.append("scan") or True,
        )
    self.assertEqual(starts, [])

def test_reenable_triggers_one_immediate_scan(self):
    state = _new_reflow_scheduler_state(previous_enabled=False)
    starts = []
    state = web_ui._reflow_scheduler_step(
        datetime(2026, 7, 30, 10, 10, tzinfo=BEIJING),
        {"auto_scan_enabled": True},
        state,
        lambda: starts.append("scan") or True,
    )
    self.assertEqual(starts, ["scan"])

def test_busy_slot_is_recorded_as_skipped_without_queue(self):
    state = _new_reflow_scheduler_state(
        previous_enabled=True,
        next_scan_at=milliseconds("2026-07-30T02:03:00Z"),
    )
    state = web_ui._reflow_scheduler_step(
        datetime(2026, 7, 30, 10, 3, tzinfo=BEIJING),
        {"auto_scan_enabled": True},
        state,
        lambda: False,
    )
    self.assertEqual(state["last_skip_at"], milliseconds("2026-07-30T02:03:00Z"))
    self.assertEqual(state["next_scan_at"], milliseconds("2026-07-30T03:03:00Z"))
```

Also assert `_start_reflow_scheduler()` called twice creates one thread.

- [ ] **Step 5: Run scheduler tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_scheduler -v
```

Expected: ERROR because scheduler runtime and lifecycle functions do not exist.

- [ ] **Step 6: Implement the single scheduler**

Keep scheduler lifecycle in `web_ui.py` and pure due-time logic in Task 2's module:

```python
_reflow_scheduler_thread = None
_reflow_scheduler_stop = threading.Event()
_reflow_automation = {
    "running": False,
    "last_auto_scan_at": 0,
    "last_auto_error": "",
    "last_skip_at": 0,
    "next_scan_at": 0,
}
```

The loop checks settings at most once per second. Track the previous enabled value:

- first observed enabled -> immediate attempt once;
- disabled -> enabled -> immediate attempt once;
- due at `HH:03` -> one attempt for that hour's slot;
- busy worker -> record skip and advance to next `HH:03`;
- disabled -> no call to the scanner.

`_reflow_scheduler_step()` is the only code that decides whether an attempt is due. It returns a copied state dictionary containing `previous_enabled`, `last_attempt_slot`, `last_skip_at`, and epoch-millisecond `next_scan_at`. The background loop only loads settings, calls this pure step, copies its public fields into `_reflow_automation`, and waits on `_reflow_scheduler_stop` for at most one second. Mark the attempted slot before calling `start_scan` so a busy worker cannot create a retry loop.

Do not start the scheduler merely by importing `web_ui.py`. Call `_start_reflow_scheduler()` in the actual `if __name__ == "__main__"` path before `app.run()`. Tests call the lifecycle functions explicitly.

Expose:

```python
@app.route("/api/reflow/automation/status")
def reflow_automation_status():
    settings = load_reflow_settings(MOMENTUM_REFLOW_SETTINGS)
    return jsonify({
        **_reflow_automation,
        "auto_scan_enabled": settings["auto_scan_enabled"],
    })
```

Invalid settings return HTTP 503 with `auto_scan_enabled=False` and a concise error; manual scan remains available.

- [ ] **Step 7: Run focused scheduler and integration tests**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_scheduler -v
python -m unittest tests.test_momentum_reflow_integration -v
python -m py_compile web_ui.py
```

Expected: all tests pass; no test performs a real Binance request or sleeps for wall-clock minutes.

- [ ] **Step 8: Commit**

```powershell
git add web_ui.py tests/test_momentum_reflow_integration.py tests/test_momentum_reflow_scheduler.py
git commit -m "feat: schedule hourly momentum reflow scans"
```

---

### Task 4: Build the Persistent Today Dashboard

**Files:**
- Modify: `web_ui.py:600-1100` (existing CSS block; append reflow-specific classes)
- Modify: `web_ui.py:1350-1390` (menu description)
- Modify: `web_ui.py:1594-1660` (`renderMomentumReflow`)
- Modify: `web_ui.py:2250-2400` (responsive styles and polling integration; use actual final locations)
- Modify: `tests/test_momentum_reflow_integration.py`

**Interfaces:**
- Consumes the Task 2 dashboard payload:

```json
{
  "rows": [],
  "scanned": 0,
  "errors": 0,
  "initialized": 0,
  "today_total": 0,
  "high_quality_count": 0,
  "automation": {}
}
```

- Produces client functions `renderMomentumReflow(payload)`, `setReflowFilter(name, value)`, and `reflowFreshness(returnCloseTime)`.

- [ ] **Step 1: Write failing dashboard-render tests**

Extend the Node DOM test:

```python
def test_dashboard_sorts_freshness_and_renders_quality_status_and_type(self):
    payload = {
        "rows": [
            row(symbol="OLDUSDT", return_open_time=1000, quality_score=99),
            row(symbol="NEWUSDT", return_open_time=2000, quality_score=60),
        ],
        "today_total": 2,
        "high_quality_count": 1,
        "automation": {
            "auto_scan_enabled": True,
            "last_auto_scan_at": 3000,
            "next_scan_at": 4000,
        },
    }
    html = render_with_node(payload)
    self.assertLess(html.index("NEW"), html.index("OLD"))
    for text in ("高质量", "标准", "商品", "回流有效", "下次扫描"):
        self.assertIn(text, html)

def test_dashboard_filters_do_not_mutate_source_rows(self):
    # Select HIGH and LONG; assert visible HTML changes while original payload length stays fixed.

def test_mobile_markup_contains_expandable_details(self):
    self.assertIn("reflow-mobile-details", html)
```

Retain existing escaping, `NaN`, `Infinity`, empty state, LONG/SHORT, and Beijing-time assertions.

- [ ] **Step 2: Run UI tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_integration.MomentumReflowUiTests -v
```

Expected: FAIL because current renderer has no persistent status cards, quality filters, instrument types, or expandable mobile details.

- [ ] **Step 3: Implement the dashboard state and stable client sorting**

Keep a page-local filter object:

```javascript
var _reflowFilters={quality:'ALL',direction:'ALL',type:'ALL',status:'ALL'};
var _reflowPayload={rows:[]};
```

Before rendering, copy the rows with `slice()` and sort:

```javascript
rows.sort(function(a,b){
  return finiteRNumber(b.return_open_time)-finiteRNumber(a.return_open_time)
    || finiteRNumber(b.quality_score)-finiteRNumber(a.quality_score)
    || finiteRNumber(b.breakout_volume_ratio)-finiteRNumber(a.breakout_volume_ratio)
    || String(a.symbol).localeCompare(String(b.symbol));
});
```

Do not sort the payload array in place.

Render eight compact status cards:

- automatic state;
- last scan;
- next scan;
- current scan state and progress;
- scanned count;
- error count;
- today total;
- high quality count.

Use label maps, never raw payload HTML:

```javascript
var qualityLabel={HIGH:'高质量',STANDARD:'标准',WATCH:'观察'};
var typeLabel={CRYPTO:'加密',COMMODITY:'商品',FX:'外汇'};
var statusLabel={ACTIVE:'回流有效',WINDOW_COMPLETE:'窗口结束',INVALID:'事件失效'};
```

Freshness uses `return_close_time` and displays `刚刚`, `N分钟前`, or `N小时前`; it must not change sorting.

- [ ] **Step 4: Add restrained AXIOM styling and mobile details**

Use existing Titanium/Linear variables and only `transform`/`opacity` animations. Add:

- quality badge colors;
- active/terminal status chips;
- filter bar;
- compact status grid;
- table desktop columns;
- `<details class="reflow-mobile-details">` for secondary fields below 768px.

Do not change existing IDs/classes used by other pages.

- [ ] **Step 5: Run UI and compile checks**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_integration.MomentumReflowUiTests -v
python -m py_compile web_ui.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add web_ui.py tests/test_momentum_reflow_integration.py
git commit -m "feat: show persistent reflow signal dashboard"
```

---

### Task 5: Add the Administrator Auto-Scan Switch

**Files:**
- Modify: `admin_server.py:1-30`
- Modify: `admin_server.py:137-188`
- Modify: `admin_server.py:270-277`
- Modify: `admin_server.py:307-330`
- Modify: `admin_server.py:464-600`
- Create: `tests/test_momentum_reflow_admin.py`

**Interfaces:**
- Consumes: `load_reflow_settings()` and `save_reflow_settings()` from Task 2.
- Produces: authenticated `GET /api/reflow/settings`
- Produces: authenticated `POST /api/reflow/settings`
- Reads scheduler status from `http://127.0.0.1:5000/api/reflow/automation/status`.

- [ ] **Step 1: Write failing administrator API tests**

Create:

```python
class MomentumReflowAdminTests(unittest.TestCase):
    def test_unauthenticated_get_and_post_are_rejected(self):
        self.assertEqual(self.client.get("/api/reflow/settings").status_code, 403)
        self.assertEqual(self.client.post(
            "/api/reflow/settings", json={"auto_scan_enabled": False}
        ).status_code, 403)

    def test_admin_can_disable_and_persist_switch(self):
        login_admin(self.client)
        response = self.client.post(
            "/api/reflow/settings",
            json={"auto_scan_enabled": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(load_reflow_settings(self.path)["auto_scan_enabled"])

    def test_post_rejects_non_boolean_value(self):
        login_admin(self.client)
        response = self.client.post(
            "/api/reflow/settings",
            json={"auto_scan_enabled": "false"},
        )
        self.assertEqual(response.status_code, 400)
```

Mock the web status request and verify GET combines persisted settings with `last_auto_scan_at`, `next_scan_at`, and `last_auto_error`. Verify a status proxy failure returns settings plus `scheduler_status="unavailable"` rather than changing the switch.

- [ ] **Step 2: Run admin tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_admin -v
```

Expected: FAIL with HTTP 404 because the routes do not exist.

- [ ] **Step 3: Implement authenticated settings routes**

Add paths:

```python
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_REFLOW_SETTINGS_PATH = Path(_BASE_DIR) / "momentum_reflow_settings.json"
```

Implement:

```python
@app.route("/api/reflow/settings", methods=["GET", "POST"])
@admin_required
def api_reflow_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        enabled = data.get("auto_scan_enabled")
        if type(enabled) is not bool:
            return jsonify({"error": "auto_scan_enabled must be boolean"}), 400
        saved = save_reflow_settings(
            _REFLOW_SETTINGS_PATH,
            enabled,
            session.get("admin_id", ""),
            int(time.time() * 1000),
        )
        return jsonify({"ok": True, **saved})
    settings = load_reflow_settings(_REFLOW_SETTINGS_PATH)
    try:
        status = _requests.get(
            f"{_WEB_UI}/api/reflow/automation/status", timeout=3
        ).json()
    except Exception:
        status = {"scheduler_status": "unavailable"}
    return jsonify({**settings, **status})
```

Do not write `demo_bot_config.json` and do not accept a volume threshold from this endpoint.

- [ ] **Step 4: Write failing admin HTML contract tests**

Assert:

- sidebar contains `动能回流`;
- panel contains checkbox `reflowAutoEnabled`;
- current state, last scan, next scan, and error elements exist;
- toggle POST sends a JSON boolean, not a string;
- failed save restores the previous checkbox state and shows an error.

- [ ] **Step 5: Run HTML tests and verify RED**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_admin -v
```

Expected: API tests pass but HTML contract assertions fail.

- [ ] **Step 6: Add the administrator panel**

Add one sidebar entry:

```html
<div class="nav-item" data-page="reflow" onclick="switchPage('reflow')">↺ 动能回流</div>
```

Render a focused card containing:

- global enabled switch;
- fixed `200万 USDT` liquidity text;
- last settings modification time and modifier;
- last automatic scan;
- next scan;
- recent error;
- explanatory text that disabling does not remove history and manual scan stays available.

The save handler sends:

```javascript
body:JSON.stringify({auto_scan_enabled:Boolean(box.checked)})
```

On failure, restore the previous checked value and render the returned error. Do not use a complex inline `onclick`; bind the handler in the render function.

- [ ] **Step 7: Run admin and existing config tests**

Run:

```powershell
python -m unittest tests.test_momentum_reflow_admin -v
python -m unittest tests.test_predicta_config -v
python -m py_compile admin_server.py
```

Expected: all tests pass and existing demo/Predicta controls remain unchanged.

- [ ] **Step 8: Commit**

```powershell
git add admin_server.py tests/test_momentum_reflow_admin.py
git commit -m "feat: control reflow automation from admin"
```

---

### Task 6: Whole-Feature Verification and Deployment Readiness

**Files:**
- Modify only if verification exposes a defect directly caused by Tasks 1-5.
- Do not update `PROGRESS.md` until an actual server deployment succeeds.

**Interfaces:**
- Verifies the complete design contract and produces a clean reviewed branch.

- [ ] **Step 1: Run all focused tests**

Run:

```powershell
python -m unittest tests.test_momentum_reflow -v
python -m unittest tests.test_momentum_reflow_dashboard -v
python -m unittest tests.test_momentum_reflow_scheduler -v
python -m unittest tests.test_momentum_reflow_integration -v
python -m unittest tests.test_momentum_reflow_admin -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the full regression suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Run compile and repository checks**

Run:

```powershell
python -m py_compile momentum_reflow.py momentum_reflow_dashboard.py web_ui.py admin_server.py
git diff --check
git status --short
```

Expected: compile and diff checks exit 0; only intentional committed feature changes exist.

- [ ] **Step 4: Perform local real-browser validation**

Start the local Flask services without connecting an exchange trading client. Use Playwright CLI to verify:

- today dashboard loads persisted fixture rows;
- newest return appears before an older but higher-score row;
- HIGH/LONG/COMMODITY/ACTIVE filters work;
- mobile details expand;
- administrator switch loads and sends a boolean;
- disabling leaves manual scan visible;
- browser console has no new error other than any pre-existing favicon 404.

Do not trigger a real all-market scan during browser layout validation.

- [ ] **Step 5: Request whole-branch code review**

Generate a review package from the feature branch merge base through `HEAD`. Review exact compliance with:

- universe exclusions;
- 2,000,000 inclusive threshold;
- score math;
- Beijing day boundary;
- persistent dedupe;
- no scan overlap;
- scheduler off behavior;
- administrator authorization;
- no trading-path changes.

Fix every Critical and Important finding, rerun covering tests, and re-review until the verdict is `Ready to merge: Yes`.

- [ ] **Step 6: Stop before production deployment**

Report:

- commits;
- focused and full test counts;
- browser result;
- review verdict;
- exact production files required: `momentum_reflow.py`, `momentum_reflow_dashboard.py`, `web_ui.py`, and `admin_server.py`.

Request explicit deployment approval. Deployment, backups, service restarts, live scan initialization, and `PROGRESS.md` updates are not authorized by plan execution alone.
