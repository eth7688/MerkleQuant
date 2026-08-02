# Demo Daily Pattern Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the previous closed Bitget `1Dutc` candle pattern for every enabled demo-engine entry source and safely backfill the records missed since the feature's first production deployment.

**Architecture:** Move the source-independent observation decision into a small `SqueezeBreakoutBot` helper that returns an immutable snapshot plus the existing RJ-only soft-block decision. Add a one-time, dry-run-first migration tool that evaluates each eligible record at its original entry timestamp, preserves untouched JSONL lines, and atomically replaces state files only when every target succeeds.

**Tech Stack:** Python 3.12, `unittest`, pandas, existing `trader.py`/`strategy_filters.py` candle evaluator, SQLite-independent JSON/JSONL state, systemd.

## Global Constraints

- Demo remains `rj_daily_pattern_filter_mode="log_only"`; this change must not block entries.
- Other engines remain `off` unless explicitly configured.
- Existing `soft` blocking remains limited to `source_strategy == "rj_only"`.
- All pattern decisions use Bitget `1Dutc` and `candle_close_time <= entry decision_time`.
- Backfill cutoff is exactly `2026-08-01T08:10:32Z` (`1785571832000` milliseconds).
- Only missing or `not_recorded` snapshots at/after the cutoff are eligible.
- Existing recorded snapshots and pre-cutoff history are immutable.
- Closed trades derive entry time only from a unique `signal_key` match to an `entry_filled` event in `signal_events_0.jsonl`; trade `time` is exit time and must never drive pattern evaluation.
- Any target fetch/evaluation failure aborts the whole migration before state-file replacement.
- No external dependency or frontend change is allowed.

---

### Task 1: Make Daily Pattern Observation Source-Independent

**Files:**
- Modify: `trader.py:1722-1747`
- Modify: `trader.py:6494-6509`
- Test: `tests/test_daily_pattern_shadow.py`

**Interfaces:**
- Consumes: `SqueezeBreakoutBot._daily_pattern_state_for_entry(symbol: str, direction: str, decision_time: int | None = None) -> dict`
- Produces: `SqueezeBreakoutBot._daily_pattern_entry_decision(symbol: str, direction: str, source_strategy: str, decision_time: int | None = None) -> tuple[dict | None, bool]`
- The tuple is `(snapshot, should_block)`. `snapshot is None` only when mode is `off`; `should_block` may be true only for RJ-only soft mode.

- [ ] **Step 1: Write failing tests for Predicta observation, off mode, and RJ soft preservation**

Add to `DailyPatternLiveAuditTest`:

```python
    def test_predicta_entry_observes_daily_pattern_when_log_only(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "log_only"
        snapshot = {"recorded": True, "kind": "hammer", "mode": "log_only",
                    "would_block": False}
        with patch.object(bot, "_daily_pattern_state_for_entry", return_value=snapshot) as observe:
            state, blocked = bot._daily_pattern_entry_decision(
                "TESTUSDT", "LONG", "predicta_ewo", decision_time=123,
            )
        observe.assert_called_once_with("TESTUSDT", "LONG", decision_time=123)
        self.assertIs(state, snapshot)
        self.assertFalse(blocked)

    def test_off_mode_skips_daily_fetch_for_every_source(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "off"
        with patch.object(bot, "_daily_pattern_state_for_entry") as observe:
            state, blocked = bot._daily_pattern_entry_decision(
                "TESTUSDT", "SHORT", "predicta_ewo", decision_time=123,
            )
        observe.assert_not_called()
        self.assertIsNone(state)
        self.assertFalse(blocked)

    def test_soft_block_remains_rj_only(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "soft"
        opposed = {"recorded": True, "kind": "bearish_engulfing", "mode": "soft",
                   "would_block": True}
        with patch.object(bot, "_daily_pattern_state_for_entry", return_value=opposed):
            _, predicta_block = bot._daily_pattern_entry_decision(
                "TESTUSDT", "LONG", "predicta_ewo", decision_time=123,
            )
            _, rj_block = bot._daily_pattern_entry_decision(
                "TESTUSDT", "LONG", "rj_only", decision_time=123,
            )
        self.assertFalse(predicta_block)
        self.assertTrue(rj_block)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_shadow.py -q
```

Expected: the three new tests fail because `_daily_pattern_entry_decision` does not exist.

- [ ] **Step 3: Add an explicit decision time and source-independent decision helper**

Change `_daily_pattern_state_for_entry` to accept the optional decision time:

```python
    def _daily_pattern_state_for_entry(
        self, symbol: str, direction: str, decision_time: Optional[int] = None,
    ) -> dict:
        mode = str(getattr(self.cfg, "rj_daily_pattern_filter_mode", "off") or "off").strip().lower()
        if mode not in ("off", "log_only", "soft"):
            mode = "log_only"
        if mode == "off":
            return self._position_daily_pattern({"reason": "disabled", "mode": mode})
        try:
            daily = fetch_klines(
                symbol, "1d", 50,
                exchange="bitget",
                closed_only=True,
                bitget_granularity="1Dutc",
            )
        except Exception:
            return self._position_daily_pattern({
                "reason": "daily_fetch_failed",
                "mode": mode,
            })
        state = evaluate_daily_pattern_state(
            daily,
            direction,
            decision_time=(int(time.time() * 1000) if decision_time is None else decision_time),
        )
        state["mode"] = mode
        return self._position_daily_pattern(state)

    def _daily_pattern_entry_decision(
        self, symbol: str, direction: str, source_strategy: str,
        decision_time: Optional[int] = None,
    ) -> tuple[Optional[dict], bool]:
        mode = str(getattr(self.cfg, "rj_daily_pattern_filter_mode", "off") or "off").strip().lower()
        if mode == "off":
            return None, False
        snapshot = self._daily_pattern_state_for_entry(
            symbol, direction, decision_time=decision_time,
        )
        should_block = (
            source_strategy == "rj_only"
            and snapshot.get("mode") == "soft"
            and snapshot.get("would_block")
        )
        return snapshot, bool(should_block)
```

- [ ] **Step 4: Wire the helper into the common entry path**

Replace the RJ-only observation block with:

```python
        daily_pattern, daily_pattern_block = self._daily_pattern_entry_decision(
            symbol,
            direction,
            source_strategy,
            decision_time=int(time.time() * 1000),
        )
        if daily_pattern is not None:
            signal["daily_pattern"] = daily_pattern
            self._append_signal_event(
                "rj_daily_pattern_shadow",
                symbol,
                self._signal_snapshot(signal, {"daily_pattern": daily_pattern}),
            )
        if daily_pattern_block:
            self._append_signal_event("entry_reject", symbol, self._signal_snapshot(signal, {
                "reason": "daily_pattern_opposed",
                "daily_pattern": daily_pattern,
            }))
            return None
```

- [ ] **Step 5: Run focused and entry-chain regression tests**

Run:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_shadow.py -q
python -m unittest discover -s tests -p 'test_*entry*.py' -q
python -m py_compile trader.py strategy_filters.py web_ui.py
```

Expected: all selected tests pass and compilation exits `0`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add trader.py tests/test_daily_pattern_shadow.py
git commit -m "fix: record daily pattern for every demo entry source"
```

---

### Task 2: Add an Idempotent Point-in-Time Backfill Tool

**Files:**
- Create: `tools/backfill_demo_daily_patterns.py`
- Create: `tests/test_daily_pattern_backfill.py`
- Read: `strategy_filters.py:28-100`

**Interfaces:**
- Produces: `parse_time_ms(value: object) -> int | None`
- Produces: `build_entry_time_index(event_lines: list[str]) -> dict[str, int]`
- Produces: `backfill_records(positions: list[dict], trade_lines: list[str], entry_times: dict[str, int], cutoff_ms: int, history_loader: Callable[[str], pd.DataFrame]) -> tuple[list[dict], list[str], dict]`
- Produces CLI: `python tools/backfill_demo_daily_patterns.py --root PATH [--cutoff ISO] [--apply]`
- Report keys: `targets`, `updated_positions`, `updated_trades`, `skipped_pre_cutoff`, `skipped_recorded`, `failures`, and `snapshots`.

- [ ] **Step 1: Write failing migration tests**

Create `tests/test_daily_pattern_backfill.py` with these cases:

```python
import copy
import json
import unittest
from datetime import datetime, timezone

import pandas as pd

from tools.backfill_demo_daily_patterns import (
    backfill_records, build_entry_time_index, parse_time_ms,
)


DAY_MS = 86_400_000
CUTOFF = 1_785_571_832_000


def frame_with_future_pattern():
    return pd.DataFrame({
        "ot": [CUTOFF - 3 * DAY_MS, CUTOFF - 2 * DAY_MS,
               CUTOFF - DAY_MS, CUTOFF + DAY_MS],
        "o": [100.0, 101.0, 98.5, 102.0],
        "h": [101.0, 102.0, 102.0, 103.0],
        "l": [99.0, 98.0, 98.0, 97.0],
        "c": [100.0, 99.0, 101.5, 98.0],
        "v": [100.0] * 4,
    })


def missing(symbol="TESTUSDT", entry_ms=CUTOFF + 1_000):
    return {
        "symbol": symbol,
        "direction": "LONG",
        "entry_time": datetime.fromtimestamp(entry_ms / 1000, timezone.utc).isoformat(),
        "entry_price": 100.0,
        "daily_pattern": {"recorded": False, "reason": "not_recorded"},
    }


def closed_trade(signal_key="SIG-1", exit_ms=CUTOFF + DAY_MS):
    row = missing(entry_ms=CUTOFF + 1_000)
    row.pop("entry_time")
    row["time"] = datetime.fromtimestamp(exit_ms / 1000, timezone.utc).isoformat()
    row["signal_key"] = signal_key
    return row


class DailyPatternBackfillTest(unittest.TestCase):
    def test_parse_time_accepts_iso_and_milliseconds(self):
        self.assertEqual(parse_time_ms(CUTOFF), CUTOFF)
        self.assertEqual(parse_time_ms("2026-08-01T08:10:32+00:00"), CUTOFF)

    def test_backfill_uses_entry_time_and_ignores_future_candle(self):
        position = missing()
        positions, lines, report = backfill_records(
            [position], [], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(report["updated_positions"], 1)
        self.assertEqual(positions[0]["daily_pattern"]["kind"], "bullish_engulfing")
        self.assertLessEqual(
            positions[0]["daily_pattern"]["candle_close_time"],
            parse_time_ms(position["entry_time"]),
        )

    def test_pre_cutoff_and_recorded_rows_are_unchanged(self):
        old = missing(entry_ms=CUTOFF - 1)
        recorded = missing(symbol="DONEUSDT")
        recorded["daily_pattern"] = {"recorded": True, "kind": "hammer"}
        original = copy.deepcopy([old, recorded])
        positions, _, report = backfill_records(
            [old, recorded], [], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(positions, original)
        self.assertEqual(report["targets"], 0)

    def test_trade_jsonl_preserves_untouched_lines_and_is_idempotent(self):
        untouched = json.dumps(missing(entry_ms=CUTOFF - 1), ensure_ascii=False)
        target = json.dumps(closed_trade(), ensure_ascii=False)
        positions, lines, first = backfill_records(
            [], [untouched, target], {"SIG-1": CUTOFF + 1_000},
            CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(lines[0], untouched)
        _, second_lines, second = backfill_records(
            positions, lines, {"SIG-1": CUTOFF + 1_000},
            CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(second_lines, lines)
        self.assertEqual(second["targets"], 0)

    def test_any_loader_failure_raises_before_mutating_inputs(self):
        positions = [missing()]
        original = copy.deepcopy(positions)
        with self.assertRaises(RuntimeError):
            backfill_records(
                positions, [], {}, CUTOFF,
                lambda _: (_ for _ in ()).throw(RuntimeError("network")),
            )
        self.assertEqual(positions, original)

    def test_trade_uses_entry_event_not_exit_time(self):
        event = json.dumps({
            "event": "entry_filled", "time": "2026-08-01T08:10:33+00:00",
            "signal_key": "SIG-1",
        })
        index = build_entry_time_index([event])
        trade = json.dumps(closed_trade(exit_ms=CUTOFF + 10 * DAY_MS))
        _, lines, report = backfill_records(
            [], [trade], index, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        migrated = json.loads(lines[0])
        self.assertEqual(index["SIG-1"], CUTOFF + 1_000)
        self.assertLessEqual(
            migrated["daily_pattern"]["candle_close_time"], index["SIG-1"],
        )

    def test_recent_trade_without_unique_entry_event_aborts(self):
        trade = json.dumps(closed_trade(signal_key="MISSING"))
        with self.assertRaises(RuntimeError):
            backfill_records(
                [], [trade], {}, CUTOFF, lambda _: frame_with_future_pattern(),
            )
```

- [ ] **Step 2: Run migration tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_backfill.py -q
```

Expected: import failure because `tools.backfill_demo_daily_patterns` does not exist.

- [ ] **Step 3: Implement pure transformation and point-in-time evaluation**

Implement `tools/backfill_demo_daily_patterns.py` with:

```python
DEFAULT_CUTOFF = "2026-08-01T08:10:32+00:00"


def parse_time_ms(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def build_entry_time_index(event_lines):
    index = {}
    for raw in event_lines:
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("event") != "entry_filled":
            continue
        snapshot = event.get("snapshot") or event.get("data") or {}
        key = str(event.get("signal_key") or snapshot.get("signal_key") or "").strip()
        entry_ms = parse_time_ms(event.get("time"))
        if not key or entry_ms is None:
            continue
        if key in index and index[key] != entry_ms:
            raise RuntimeError(f"non-unique entry event for {key}")
        index[key] = entry_ms
    return index


def _entry_time_for_row(kind, row, entry_times, cutoff_ms):
    if kind == "position":
        return parse_time_ms(row.get("entry_time"))
    key = str(row.get("signal_key", "") or "").strip()
    if key and key in entry_times:
        return entry_times[key]
    exit_ms = parse_time_ms(row.get("time"))
    if exit_ms is not None and exit_ms >= cutoff_ms:
        raise RuntimeError(
            f"recent trade has no unique entry event: {row.get('symbol')}/{key}"
        )
    return None


def _eligible(row, entry_ms, cutoff_ms):
    pattern = row.get("daily_pattern") or {}
    reason = str(pattern.get("reason", "not_recorded") or "not_recorded")
    return (
        entry_ms is not None
        and entry_ms >= cutoff_ms
        and not bool(pattern.get("recorded"))
        and reason in ("", "not_recorded")
    ), entry_ms


def _snapshot(row, entry_ms, daily):
    state = evaluate_daily_pattern_state(
        daily,
        str(row.get("direction", "")),
        decision_time=entry_ms,
    )
    if not state.get("recorded"):
        raise RuntimeError(
            f"{row.get('symbol')} at {entry_ms}: {state.get('reason', 'unavailable')}"
        )
    state["mode"] = "log_only"
    return state


def backfill_records(positions, trade_lines, entry_times, cutoff_ms, history_loader):
    new_positions = copy.deepcopy(positions)
    parsed_trades = [(line, json.loads(line)) for line in trade_lines if line.strip()]
    targets = []
    for kind, rows in (("position", new_positions),
                       ("trade", [row for _, row in parsed_trades])):
        for index, row in enumerate(rows):
            entry_ms = _entry_time_for_row(
                kind, row, entry_times, cutoff_ms,
            )
            eligible, entry_ms = _eligible(row, entry_ms, cutoff_ms)
            if eligible:
                targets.append((kind, index, row, entry_ms))

    histories = {}
    snapshots = []
    planned = []
    for kind, index, row, entry_ms in targets:
        symbol = str(row.get("symbol", "")).strip()
        direction = str(row.get("direction", "")).strip().upper()
        if not symbol or direction not in ("LONG", "SHORT"):
            raise RuntimeError(f"invalid target record: {symbol}/{direction}")
        if symbol not in histories:
            histories[symbol] = history_loader(symbol)
        state = _snapshot(row, entry_ms, histories[symbol])
        planned.append((kind, index, state))
        snapshots.append({"kind": kind, "symbol": symbol,
                          "entry_time": entry_ms, "daily_pattern": state})

    updated_positions = updated_trades = 0
    for kind, index, state in planned:
        if kind == "position":
            new_positions[index]["daily_pattern"] = state
            updated_positions += 1
        else:
            parsed_trades[index][1]["daily_pattern"] = state
            updated_trades += 1
    changed_trade_indexes = {index for kind, index, _ in planned if kind == "trade"}
    new_lines = [
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if index in changed_trade_indexes else raw
        for index, (raw, row) in enumerate(parsed_trades)
    ]
    report = {
        "targets": len(targets),
        "updated_positions": updated_positions,
        "updated_trades": updated_trades,
        "failures": 0,
        "snapshots": snapshots,
    }
    return new_positions, new_lines, report
```

The final implementation must also count `skipped_pre_cutoff` and `skipped_recorded` while scanning so the report matches the interface.

- [ ] **Step 4: Implement dry-run-first CLI and atomic writes**

The CLI must:

```python
def load_history(symbol):
    return fetch_klines(
        symbol, "1d", 50,
        exchange="bitget", closed_only=True,
        bitget_granularity="1Dutc",
    )


def atomic_write(path, text):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
```

It loads `<root>/positions_<uid>.json`, raw lines from `<root>/trades_<uid>.jsonl`, and read-only entry events from `<root>/signal_events_0.jsonl`. It builds the unique entry-time index, calls `backfill_records`, prints the JSON report in both modes, and writes only when `--apply` is present. It writes positions as indented JSON plus newline and trades as joined JSONL plus a final newline. It must refuse `--apply` unless all three paths exist and `report["targets"] > 0`.

- [ ] **Step 5: Run migration tests twice and verify GREEN/idempotency**

Run:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_backfill.py -q
python -m unittest discover -s tests -p test_daily_pattern_backfill.py -q
python -m py_compile tools/backfill_demo_daily_patterns.py
```

Expected: all tests pass on both runs and compilation exits `0`.

- [ ] **Step 6: Commit Task 2**

```powershell
git add tools/backfill_demo_daily_patterns.py tests/test_daily_pattern_backfill.py
git commit -m "feat: add point-in-time daily pattern backfill"
```

---

### Task 3: Full Regression and Migration Safety Review

**Files:**
- Modify only if verification exposes a Task 1/2 defect: `trader.py`, `tools/backfill_demo_daily_patterns.py`, or their tests

**Interfaces:**
- Consumes the Task 1 entry helper and Task 2 CLI.
- Produces a clean, deployable commit with no unrelated diff.

- [ ] **Step 1: Run the full local verification gate**

```powershell
$out = Join-Path $env:TEMP 'daily-pattern-backfill-tests.txt'
python -m unittest discover -s tests -q *> $out
$code = $LASTEXITCODE
Get-Content $out -Tail 10
Remove-Item $out
if ($code -ne 0) { exit $code }
python -m py_compile trader.py strategy_filters.py web_ui.py tools/backfill_demo_daily_patterns.py
git diff --check
git status --short
```

Expected: the complete suite reports `OK`, all compilation succeeds, `git diff --check` is clean, and status contains only planned files if a verification fix remains uncommitted.

- [ ] **Step 2: Review the migration invariants against fixtures**

Run a temporary fixture dry-run and apply:

```powershell
python -m unittest discover -s tests -p test_daily_pattern_backfill.py -v
```

Confirm from assertions that pre-cutoff and recorded rows are byte/structure stable, future candles are excluded, failure is all-or-nothing, and the second pass makes zero changes.

- [ ] **Step 3: Inspect the final diff**

```powershell
git diff HEAD~2 -- trader.py tools/backfill_demo_daily_patterns.py tests/test_daily_pattern_shadow.py tests/test_daily_pattern_backfill.py
```

Reject any change outside source-independent observation, point-in-time migration, and their tests.

- [ ] **Step 4: Commit any review fix and require a clean worktree**

```powershell
git add trader.py tools/backfill_demo_daily_patterns.py tests/test_daily_pattern_shadow.py tests/test_daily_pattern_backfill.py
git commit -m "fix: close daily pattern backfill review findings"
git status --short
```

Skip the commit command when there is no review fix. Expected final status: empty.

---

### Task 4: Production Backup, Dry Run, Apply, and Deploy

**Files:**
- Deploy: `trader.py` to `<deploy-dir>/trader.py`
- Temporary migration upload: `tools/backfill_demo_daily_patterns.py` to `/tmp/backfill_demo_daily_patterns.py`
- Read-only migration source: `<deploy-dir>/signal_events_0.jsonl`
- Mutate after backup and dry-run: `<deploy-dir>/positions_<uid>.json`, `<deploy-dir>/trades_<uid>.jsonl`
- Update locally after successful deployment: `PROGRESS.md`

**Interfaces:**
- SSH key: `<repo-root>/<ssh-key>`
- Server: `root@<production-host>`
- App root: `<deploy-dir>`
- Service: `macd-bot`

- [ ] **Step 1: Record pre-deployment hashes and create a server backup**

Stop `macd-bot` only after confirming the staged local verification is green. Then create `/root/axiom_deploy_backups/daily_pattern_backfill_<UTC timestamp>` containing `trader.py`, `positions_<uid>.json`, `trades_<uid>.jsonl`, `signal_events_0.jsonl`, `demo_bot_config.json`, and `axiom_accounts.db`. Record SHA256 for all six files and the position/trade counts.

- [ ] **Step 2: Upload explicit staged files and compile before replacement**

```powershell
scp -i <repo-root>/<ssh-key> trader.py root@<production-host>:<deploy-dir>/trader.py.codex-new
scp -i <repo-root>/<ssh-key> tools/backfill_demo_daily_patterns.py root@<production-host>:/tmp/backfill_demo_daily_patterns.py
```

On the server, compare the staged `trader.py` SHA256 with local and compile both files using `<deploy-dir>/venv/bin/python3` without replacing production code.

- [ ] **Step 3: Run production dry-run and verify every target**

```bash
cd <deploy-dir>
PYTHONPATH=<deploy-dir> venv/bin/python3 /tmp/backfill_demo_daily_patterns.py \
  --root <deploy-dir> \
  --cutoff 2026-08-01T08:10:32+00:00
```

Expected: exit `0`, `failures=0`, at least the three current positions and the post-cutoff closed trades are listed, every snapshot has `recorded=true`, and every `candle_close_time <= entry_time`.

- [ ] **Step 4: Apply migration while service remains stopped**

Run the same command with `--apply`. Immediately rerun dry-run; expected `targets=0`. Compare backup and new files structurally and fail deployment unless every difference is under an eligible row's `daily_pattern` key.

- [ ] **Step 5: Atomically deploy trader and restart**

Move `trader.py.codex-new` to `trader.py`, compile the official path, restart `macd-bot`, and require `active/running`. Remove the explicit temporary file `/tmp/backfill_demo_daily_patterns.py` only after successful verification.

- [ ] **Step 6: Verify live behavior**

Require:

- `/demo/status?fast=1` and `/api/reflow/automation/status` return HTTP 200.
- Current position payload contains `daily_pattern.recorded=true` for every migrated target.
- Migrated trade records contain the same entry-time snapshots.
- `journalctl -u macd-bot --since <restart timestamp> -p err..alert` has no entries.
- `demo_bot_config.json` and `axiom_accounts.db` hashes equal pre-deploy hashes.
- Positions/trades structural diff is restricted to approved `daily_pattern` fields.

- [ ] **Step 7: Update progress and commit deployment evidence**

Append to `PROGRESS.md`: root cause, code commits, test counts, backup path, dry-run/apply counts, migrated symbols/timestamps/patterns, hashes, structural-diff result, deployed hash, service/HTTP/journal results, and rollback status. Do not upload `PROGRESS.md` to the server.

```powershell
git add PROGRESS.md
git commit -m "docs: record daily pattern backfill deployment"
git status --short
```

Expected: clean worktree.
