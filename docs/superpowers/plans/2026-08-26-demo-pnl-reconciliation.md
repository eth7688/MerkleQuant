# Demo PnL Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new Binance demo-engine exits use exchange fill PnL and fees while preserving legacy estimates and preventing valid partial/final exits from being removed during restart loading.

**Architecture:** Extend `BinanceClient` with a read-only close-fill resolver that returns the same normalized fields already consumed by the trading engine. Give every new trade record a unique ID, use a richer compatibility key for legacy records, and make backend/frontend statistics prefer exchange net profit without rewriting historical JSONL data.

**Tech Stack:** Python 3.12, `unittest`, Flask JSON summaries, native browser JavaScript, Binance USD-M Futures REST API.

## Global Constraints

- Do not rewrite `trades_<uid>.jsonl` or fabricate exchange values for historical estimate records.
- Do not change entry, exit, sizing, leverage, stop-loss, or risk rules.
- Do not allocate account-level funding fees to individual partial or final exits.
- A failed fill lookup must preserve the completed close and retain an explicitly estimated PnL source.
- No external Python or frontend dependencies.

---

### Task 1: Trade Record Identity and Legacy Loading

**Files:**
- Create: `tests/test_demo_pnl_reconciliation.py`
- Modify: `trader.py:1219-1235`
- Modify: `trader.py:2117-2130`

**Interfaces:**
- Consumes: JSON-compatible trade dictionaries and `self._trade_log_path`.
- Produces: `SqueezeBreakoutBot._trade_record_key(record: dict) -> tuple` and new records containing a non-empty `record_id`.

- [ ] **Step 1: Write failing tests for legacy loading and new IDs**

```python
import json
import logging
import tempfile
import unittest
from pathlib import Path

from trader import SqueezeBreakoutBot


def bare_bot(path):
    bot = object.__new__(SqueezeBreakoutBot)
    bot.trade_log = []
    bot._trade_log_path = str(path)
    bot._log_ready = False
    bot._log = logging.getLogger("demo-pnl-reconciliation-test")
    bot._btc_regime_cache = {"ts": 0, "data": {}}
    bot._restore_recent_closed_positions_from_trades = lambda: None
    bot._restore_used_signal_keys_from_trades = lambda: None
    return bot


class TradeRecordIdentityTests(unittest.TestCase):
    def test_legacy_partial_and_final_exit_in_same_second_are_both_loaded(self):
        partial = {
            "time": "2026-08-25T02:44:59+00:00", "symbol": "PENGUUSDT",
            "direction": "SHORT", "reason": "二阶减仓50%", "quantity": 103839.0,
            "pnl": 351.5, "signal_key": "PREDICTA|PENGU|SHORT|30m|1",
        }
        final = {**partial, "reason": "击穿动态追踪防线"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.jsonl"
            path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [partial, final]), encoding="utf-8")
            bot = bare_bot(path)
            bot._load_trade_history()
        self.assertEqual([x["reason"] for x in bot.trade_log], ["二阶减仓50%", "击穿动态追踪防线"])

    def test_exact_legacy_duplicate_is_loaded_once(self):
        row = {"time": "2026-08-25T03:00:00+00:00", "symbol": "BTCUSDT", "direction": "LONG", "reason": "SL", "quantity": 1, "pnl": -10}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.jsonl"
            path.write_text("\n".join(json.dumps(row) for _ in range(2)), encoding="utf-8")
            bot = bare_bot(path)
            bot._load_trade_history()
        self.assertEqual(len(bot.trade_log), 1)

    def test_appended_records_receive_distinct_record_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = bare_bot(Path(tmp) / "trades.jsonl")
            bot._append_trade({"time": "2026-08-25T03:00:00+00:00", "symbol": "BTCUSDT", "btc_regime": "btc_unknown", "pnl": 1})
            bot._append_trade({"time": "2026-08-25T03:00:00+00:00", "symbol": "BTCUSDT", "btc_regime": "btc_unknown", "pnl": 1})
        self.assertNotEqual(bot.trade_log[0]["record_id"], bot.trade_log[1]["record_id"])
```

- [ ] **Step 2: Run the tests and verify the intended failures**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.TradeRecordIdentityTests -v`

Expected: the partial/final test loads one record and the ID test raises `KeyError: 'record_id'`.

- [ ] **Step 3: Implement the compatibility key and unique ID**

```python
import uuid

@staticmethod
def _trade_record_key(record: dict):
    record_id = str(record.get("record_id", "") or "")
    if record_id:
        return ("record_id", record_id)
    return (
        "legacy",
        str(record.get("time", "") or ""),
        str(record.get("symbol", "") or ""),
        str(record.get("direction", "") or ""),
        str(record.get("reason", "") or ""),
        str(record.get("quantity", "") or ""),
        str(record.get("pnl", "") or ""),
        str(record.get("signal_key", "") or ""),
    )
```

Use `self._trade_record_key(d)` in `_load_trade_history`. At the beginning of `_append_trade`, add:

```python
record.setdefault("record_id", uuid.uuid4().hex)
```

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.TradeRecordIdentityTests -v`

Expected: 3 tests pass.

- [ ] **Step 5: Commit the identity fix**

```bash
git add trader.py tests/test_demo_pnl_reconciliation.py
git commit -m "fix: preserve distinct demo trade records"
```

---

### Task 2: Binance Close Fill Resolution

**Files:**
- Modify: `tests/test_demo_pnl_reconciliation.py`
- Modify: `trader.py:365-425`

**Interfaces:**
- Consumes: `BinanceClient.resolve_close_trade(symbol: str, order_response: dict | None, direction: str, quantity: float) -> dict`.
- Produces: normalized `exit_price`, `filled_qty`, `exchange_pnl`, `exchange_fee`, `exchange_net_profit`, `pnl_source`, and `order_trades` fields.

- [ ] **Step 1: Add failing tests for multi-fill aggregation and missing fills**

```python
from unittest.mock import patch

from trader import BinanceClient


class BinanceCloseResolutionTests(unittest.TestCase):
    def test_resolve_close_trade_aggregates_fills_and_fees(self):
        client = BinanceClient(market_type="futures")
        fills = [
            {"orderId": 77, "price": "101", "qty": "2", "realizedPnl": "4.5", "commission": "0.08", "commissionAsset": "USDT"},
            {"orderId": 77, "price": "103", "qty": "1", "realizedPnl": "2.5", "commission": "0.04", "commissionAsset": "USDT"},
        ]
        with patch.object(client, "get_order_trades", return_value=fills):
            result = client.resolve_close_trade("BTCUSDT", {"orderId": 77}, "LONG", 3)
        self.assertAlmostEqual(result["exit_price"], 101.6666666667)
        self.assertEqual(result["filled_qty"], 3.0)
        self.assertEqual(result["exchange_pnl"], 7.0)
        self.assertEqual(result["exchange_fee"], 0.12)
        self.assertEqual(result["exchange_net_profit"], 6.88)
        self.assertEqual(result["pnl_source"], "binance_order_trades")

    def test_resolve_close_trade_returns_empty_when_order_has_no_fills(self):
        client = BinanceClient(market_type="futures")
        with patch.object(client, "get_order_trades", return_value=[]):
            self.assertEqual(client.resolve_close_trade("BTCUSDT", {"orderId": 77}, "LONG", 3), {})
```

- [ ] **Step 2: Run the tests and verify the missing-method failure**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.BinanceCloseResolutionTests -v`

Expected: FAIL because `BinanceClient` has no `get_order_trades` or `resolve_close_trade` method.

- [ ] **Step 3: Implement the read-only Binance resolver**

Add a `get_order_trades` method that calls `GET /fapi/v1/userTrades` with `symbol`, `orderId`, and `limit=1000`. Implement `resolve_close_trade` with these rules:

```python
def get_order_trades(self, symbol, order_id, limit=1000):
    if self.market_type != "futures" or not order_id:
        return []
    return self._req("GET", "/fapi/v1/userTrades", {
        "symbol": symbol,
        "orderId": int(order_id),
        "limit": min(max(int(limit), 1), 1000),
    }, signed=True) or []

def resolve_close_trade(self, symbol, order_response=None, direction="", quantity=0.0):
    order_response = order_response or {}
    order_id = order_response.get("orderId") or order_response.get("order_id")
    if self.market_type != "futures" or not order_id:
        return {}
    fills = []
    for delay in (0.0, 0.2, 0.5):
        if delay:
            time.sleep(delay)
        fills = self.get_order_trades(symbol, order_id)
        if fills:
            break
    if not fills:
        return {}
    qty_sum = sum(float(x.get("qty", 0) or 0) for x in fills)
    quote_sum = sum(float(x.get("price", 0) or 0) * float(x.get("qty", 0) or 0) for x in fills)
    realized = sum(float(x.get("realizedPnl", 0) or 0) for x in fills)
    fee = sum(float(x.get("commission", 0) or 0) for x in fills if str(x.get("commissionAsset", "USDT")) == "USDT")
    return {
        "exit_price": quote_sum / qty_sum if qty_sum > 0 else 0.0,
        "filled_qty": qty_sum,
        "exchange_pnl": realized,
        "exchange_fee": fee,
        "exchange_net_profit": realized - fee,
        "pnl_source": "binance_order_trades",
        "order_trades": fills,
    }
```

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.BinanceCloseResolutionTests -v`

Expected: 2 tests pass.

- [ ] **Step 5: Commit the Binance resolver**

```bash
git add trader.py tests/test_demo_pnl_reconciliation.py
git commit -m "fix: reconcile Binance close fills"
```

---

### Task 3: Net PnL Priority and Close-Record Integration

**Files:**
- Modify: `tests/test_demo_pnl_reconciliation.py`
- Modify: `trader.py:2342-2354`
- Verify existing integration: `trader.py:7935-7979`
- Verify existing integration: `trader.py:8260-8403`

**Interfaces:**
- Consumes: trade records containing optional `exchange_net_profit`, `exchange_pnl`, and `pnl`.
- Produces: `SqueezeBreakoutBot._trade_pnl_value(record: dict) -> float` with net-first priority.

- [ ] **Step 1: Add failing PnL-priority tests**

```python
class TradePnlPriorityTests(unittest.TestCase):
    def test_exchange_net_profit_has_highest_priority(self):
        row = {"exchange_net_profit": 6.88, "exchange_pnl": 7.0, "pnl": 8.0}
        self.assertEqual(SqueezeBreakoutBot._trade_pnl_value(row), 6.88)

    def test_exchange_pnl_precedes_estimate_when_net_is_missing(self):
        row = {"exchange_pnl": -3.0, "pnl": 9.0}
        self.assertEqual(SqueezeBreakoutBot._trade_pnl_value(row), -3.0)

    def test_estimate_remains_compatible_for_legacy_record(self):
        self.assertEqual(SqueezeBreakoutBot._trade_pnl_value({"pnl": 12.5}), 12.5)
```

- [ ] **Step 2: Run the tests and verify net-priority failure**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.TradePnlPriorityTests -v`

Expected: the first test fails with `7.0 != 6.88`.

- [ ] **Step 3: Implement net-first priority**

Change the value loop to:

```python
for key in ("exchange_net_profit", "exchange_pnl", "pnl"):
```

Confirm both partial-close and final-close record builders already copy `exchange_net_profit` returned by the normalized resolver. Do not change position risk decisions to use net PnL; this task changes reporting only.

- [ ] **Step 4: Run focused and adjacent tests**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.TradePnlPriorityTests tests.test_performance_metrics -v`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the reporting priority**

```bash
git add trader.py tests/test_demo_pnl_reconciliation.py
git commit -m "fix: prefer exchange net pnl in demo metrics"
```

---

### Task 4: Dashboard Source Labels and Summary Consistency

**Files:**
- Modify: `tests/test_demo_pnl_reconciliation.py`
- Modify: `web_ui.py:2553-2558`
- Modify: `web_ui.py:3858-3867`
- Verify: `trader.py:9244-9482`
- Verify: `trader.py:9484-9690`

**Interfaces:**
- Consumes: `/demo/status` records containing `exchange_net_profit` and `pnl_source`.
- Produces: browser `tradePnlValue(t)` using the same net-first priority as the backend and a truthful “交易所/估算” source badge.

- [ ] **Step 1: Add a failing source-contract test**

```python
class DashboardPnlContractTests(unittest.TestCase):
    def test_dashboard_prefers_exchange_net_profit(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")
        function = source[source.index("function tradePnlValue(t){"):source.index("function fmtTargetType", source.index("function tradePnlValue(t){"))]
        self.assertLess(function.index("exchange_net_profit"), function.index("exchange_pnl"))
```

- [ ] **Step 2: Run the contract test and verify failure**

Run: `python -m unittest tests.test_demo_pnl_reconciliation.DashboardPnlContractTests -v`

Expected: FAIL because `exchange_net_profit` is absent from `tradePnlValue`.

- [ ] **Step 3: Update the browser value and source detection**

Use net-first fallback:

```javascript
function tradePnlValue(t){
  if(!t || t.voided) return 0;
  var net=t.exchange_net_profit;
  if(net!==undefined && net!==null && net!=='') return Number(net)||0;
  var ex=t.exchange_pnl;
  if(ex!==undefined && ex!==null && ex!=='') return Number(ex)||0;
  return Number(t.pnl||0);
}
```

Treat a non-empty `exchange_net_profit` as an exchange-backed source in both recent-trade renderers. Preserve the legacy “估算” badge when only `pnl` exists.

- [ ] **Step 4: Run reconciliation and dashboard tests**

Run: `python -m unittest tests.test_demo_pnl_reconciliation -v`

Expected: all reconciliation tests pass.

- [ ] **Step 5: Run the full project verification**

Run: `python -m unittest discover -s tests -v`

Expected: exit code 0 with no failed tests.

Run: `python -m py_compile trader.py web_ui.py`

Expected: exit code 0 and no output.

- [ ] **Step 6: Commit the dashboard contract**

```bash
git add web_ui.py tests/test_demo_pnl_reconciliation.py
git commit -m "fix: align demo dashboard pnl source"
```

---

### Task 5: Final Evidence and Deployment Readiness

**Files:**
- Verify: `trader.py`
- Verify: `web_ui.py`
- Verify: `tests/test_demo_pnl_reconciliation.py`
- Verify: `docs/superpowers/specs/2026-08-26-demo-pnl-reconciliation-design.md`

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: a verified local commit series ready for a separately authorized production deployment.

- [ ] **Step 1: Review the exact diff and scope**

Run: `git diff HEAD~4 -- trader.py web_ui.py tests/test_demo_pnl_reconciliation.py docs/superpowers/specs/2026-08-26-demo-pnl-reconciliation-design.md`

Expected: only record identity, Binance fill resolution, net reporting priority, dashboard source handling, and their tests are changed.

- [ ] **Step 2: Re-run fresh verification**

Run: `python -m unittest discover -s tests -v`

Expected: exit code 0 with no failures.

Run: `python -m py_compile trader.py web_ui.py`

Expected: exit code 0 and no output.

- [ ] **Step 3: Confirm the worktree contains no unintended tracked changes**

Run: `git status --short`

Expected: only pre-existing untracked runtime lock files may remain; all planned tracked changes are committed.

- [ ] **Step 4: Report deployment boundary**

Report the local commit IDs, test counts, and the fact that production remains unchanged. Production upload, backup, service restart, live endpoint verification, and `PROGRESS.md` bookkeeping require a separate explicit deployment instruction.
