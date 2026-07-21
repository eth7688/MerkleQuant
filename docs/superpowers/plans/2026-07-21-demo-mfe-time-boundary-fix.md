# Demo MFE Time-Boundary Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent newly opened demo positions from using pre-entry mark-price candle extrema and immediately exiting at estimated breakeven.

**Architecture:** Keep the existing Beijing display-time convention intact. Normalize only obviously shifted entry timestamps at the UTC exchange-kline boundary, select candles opened at or after entry, and use the live mark price instead of candle extrema until such a candle exists. Mark-price MFE migration remains pending when the post-entry set is empty.

**Tech Stack:** Python 3.12, pandas, unittest, Binance mark-price klines, systemd.

## Global Constraints

- Modify only `trader.py`, the focused regression test, this plan, and post-deploy `PROGRESS.md`.
- Do not change Predicta entry rules, risk sizing, thresholds, take-profit configuration, database data, or demo configuration.
- Deploy only after local tests, full unittest discovery, compilation, and diff checks pass.
- Back up live code and state before upload; deploy only `trader.py`; do not upload memory files.

---

### Task 1: Reproduce and Fix the Post-Entry Mark-Price Boundary

**Files:**
- Modify: `tests/test_binance_kline_routing.py`
- Modify: `trader.py:6656-6821`

**Interfaces:**
- Consumes: `Position.entry_time`, mark-kline `ot/h/l/c` columns, and the current exchange mark price.
- Produces: `_entry_ms_for_market_data(entry_time, latest_open_ms) -> int`; `check_exit()` must not migrate or protect from pre-entry extrema.

- [ ] **Step 1: Write the failing regression test**

Add a test that creates a new LONG position with the current project-style Beijing wall clock carrying `timezone.utc`, supplies only candles whose open times are before the real entry, and includes pre-entry highs above `0.8R`. Patch `fetch_klines()` and assert:

```python
reason = bot.check_exit(position)
self.assertIsNone(reason)
self.assertFalse(position.breakeven_triggered)
self.assertEqual(position.excursion_price_source, "")
self.assertLess(position.max_favorable_r, cfg.early_protect_r)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
$env:PYTHONPATH=(Get-Location).Path
python tests/test_binance_kline_routing.py -v
```

Expected: the new test fails because historical MFE exceeds `0.8R`, moves `current_sl` to entry, and returns `击穿动态追踪防线`.

- [ ] **Step 3: Add the minimal timestamp boundary helper**

Add a helper beside the existing time utilities:

```python
def _entry_ms_for_market_data(entry_time: datetime, latest_open_ms: int) -> int:
    raw_ms = int(entry_time.timestamp() * 1000)
    beijing_shift_ms = 8 * 60 * 60 * 1000
    if raw_ms - int(latest_open_ms) >= 6 * 60 * 60 * 1000:
        return raw_ms - beijing_shift_ms
    return raw_ms
```

The six-hour guard distinguishes the known eight-hour display offset from a normal current UTC timestamp without changing persisted display values.

- [ ] **Step 4: Use only post-entry extrema**

In `check_exit()`:

1. Compute the normalized entry boundary from the latest kline open time.
2. Build `since_entry = df[df["ot"] >= entry_ms]`.
3. If `since_entry` is empty, calculate favorable/adverse movement from `current_price` only.
4. Rebase MFE/MAE and set `excursion_price_source="mark"` only when `since_entry` is non-empty.
5. Never fall back to the complete dataframe.

- [ ] **Step 5: Verify GREEN and existing migration behavior**

Run:

```powershell
$env:PYTHONPATH=(Get-Location).Path
python tests/test_binance_kline_routing.py -v
```

Expected: all routing and MFE migration tests pass, including the new no-pre-entry-extrema regression.

- [ ] **Step 6: Commit the code fix**

```powershell
git add trader.py tests/test_binance_kline_routing.py docs/superpowers/plans/2026-07-21-demo-mfe-time-boundary-fix.md
git commit -m "fix: reject pre-entry MFE candles"
```

---

### Task 2: Verify and Deploy the Surgical Fix

**Files:**
- Modify: `PROGRESS.md`
- Deploy: `<deploy-dir>/trader.py`

**Interfaces:**
- Consumes: locally verified `trader.py`, SSH key `<ssh-key>`, server `root@<production-host>`.
- Produces: active `macd-bot` service running the verified file and a timestamped rollback backup.

- [ ] **Step 1: Run complete local verification**

```powershell
$env:PYTHONPATH=(Get-Location).Path
python -m unittest discover -s tests -p 'test_*.py'
python -m py_compile trader.py screener.py web_ui.py admin_server.py
git diff --check
```

Expected: zero failures and zero compilation/diff errors.

- [ ] **Step 2: Back up server code and state**

Create `<deploy-dir>/backups/demo_mfe_time_boundary_<timestamp>/` containing `trader.py`, `demo_bot_config.json`, `positions_<uid>.json`, `trades_<uid>.jsonl`, and `bot_demo.log`.

- [ ] **Step 3: Upload and verify before restart**

Upload only local `trader.py`, run server `python3 -m py_compile trader.py`, and compare local/server SHA256 hashes.

- [ ] **Step 4: Restart and inspect**

Restart only `macd-bot`; require `systemctl is-active macd-bot` to return `active`. Inspect post-restart journal and `bot_demo.log` for tracebacks or startup errors. Confirm demo configuration values remain unchanged and do not print credentials.

- [ ] **Step 5: Record deployment**

Append a dated `PROGRESS.md` entry containing the root cause, exact change, tests, backup directory, deployed file, service result, checksum result, and the fact that existing trade history was not altered.

- [ ] **Step 6: Commit deployment bookkeeping**

```powershell
git add PROGRESS.md
git commit -m "docs: record demo MFE boundary deployment"
```
