# Demo Entry Order Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Binance Futures Predicta demo entries respect market-order limits and recover safely from unsupported configured leverage.

**Architecture:** Keep exchange compatibility inside `BinanceClient` and a small entry-preparation helper on `SqueezeBreakoutBot`. The signal, filter, risk, stop, and exit pipelines remain unchanged; only the final exchange-compatible quantity and leverage are prepared before the precheck-pass event and order submission.

**Tech Stack:** Python 3.12, `unittest`, `unittest.mock`, Binance Futures REST.

## Global Constraints

- `MARKET_LOT_SIZE` takes precedence for market orders; `LOT_SIZE` is fallback only.
- Leverage candidates never exceed configured leverage and stop on first success.
- Missing compatible leverage blocks the order before `entry_precheck_pass`.
- Returned `maxNotionalValue` uses a 98% safety factor before quantity flooring.
- Do not modify signal generation, EWO, choppy filtering, stop, exit, or configuration behavior.
- Deploy only `trader.py` after backup and full verification.

---

### Task 1: Market-order quantity precision

**Files:**
- Modify: `trader.py`
- Test: `tests/test_entry_order_compatibility.py`

**Interfaces:**
- Consumes: `client.get_symbol_info(symbol) -> dict`
- Produces: `SqueezeBreakoutBot._floor_qty(symbol: str, qty: float, market_order: bool = True) -> float`

- [ ] **Step 1: Write the failing test**

```python
def test_floor_qty_prefers_market_lot_size_for_market_orders(self):
    bot = _bot_with_filters(lot_max="1000000", market_max="500")
    self.assertEqual(bot._floor_qty("KAITOUSDT", 3089.0), 500.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_entry_order_compatibility.EntryOrderCompatibilityTest.test_floor_qty_prefers_market_lot_size_for_market_orders -v`

Expected: FAIL because the current implementation returns 3,089 from `LOT_SIZE`.

- [ ] **Step 3: Implement the minimal filter selection**

Collect matching filters first. For market orders select `MARKET_LOT_SIZE`, fall back to `LOT_SIZE`, then apply existing step/min/max flooring unchanged.

- [ ] **Step 4: Run the focused test**

Run the command from Step 2.

Expected: PASS.

### Task 2: Compatible Binance leverage and notional cap

**Files:**
- Modify: `trader.py`
- Test: `tests/test_entry_order_compatibility.py`

**Interfaces:**
- Produces: `BinanceClient.set_compatible_leverage(symbol: str, leverage: int) -> dict | None`
- Produces: `SqueezeBreakoutBot._prepare_futures_entry(symbol, entry_price, sl_price, qty) -> tuple[float, float, float, dict]`

- [ ] **Step 1: Write failing leverage fallback tests**

```python
def test_binance_leverage_falls_back_from_25_to_20(self):
    client = BinanceClient(market_type="futures")
    with patch.object(client, "set_leverage", side_effect=[None, {"leverage": 20, "maxNotionalValue": "2500"}]) as setter:
        result = client.set_compatible_leverage("ZAMAUSDT", 25)
    self.assertEqual([call.args[1] for call in setter.call_args_list], [25, 20])
    self.assertEqual(result["leverage"], 20)
```

```python
def test_prepare_futures_entry_caps_notional_and_recomputes_risk(self):
    qty, notional, risk, meta = bot._prepare_futures_entry("ZAMAUSDT", 10.0, 9.0, 300.0)
    self.assertEqual(qty, 245.0)
    self.assertEqual(notional, 2450.0)
    self.assertEqual(risk, 245.0)
    self.assertEqual(meta["effective_leverage"], 20)
```

```python
def test_prepare_futures_entry_blocks_when_no_leverage_is_supported(self):
    qty, notional, risk, meta = bot._prepare_futures_entry("ZAMAUSDT", 10.0, 9.0, 300.0)
    self.assertEqual(qty, 0.0)
    self.assertEqual(meta["reason"], "leverage_unavailable")
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_entry_order_compatibility -v`

Expected: FAIL because the two new methods do not exist.

- [ ] **Step 3: Implement compatible leverage**

Try unique candidates `[requested, 20, 10, 5, 3, 2, 1]` filtered to `<= requested`; return the first truthy exchange response or `None`.

- [ ] **Step 4: Implement futures entry preparation**

For Binance Futures, call the compatible setter, reject on failure, cap to `0.98 * maxNotionalValue`, floor with the market-order filter, and recompute `pos_usdt` and `risk`. Preserve the existing Bitget path.

- [ ] **Step 5: Integrate before `entry_precheck_pass`**

Call `_prepare_futures_entry` in `_enter_key_candle_position`; emit `entry_reject` with `leverage_unavailable` on failure and include compatibility metadata in successful precheck/fill events.

- [ ] **Step 6: Run focused and full verification**

Run:

```powershell
$env:PYTHONPATH=(Get-Location).Path
python -m unittest tests.test_entry_order_compatibility -v
python -m unittest discover -s tests -p 'test_*.py'
python -m py_compile trader.py web_ui.py admin_server.py
git diff --check
```

Expected: all tests PASS, compilation exits 0, and `git diff --check` is clean.

### Task 3: Deploy and verify

**Files:**
- Modify after deployment: `PROGRESS.md`
- Deploy: `trader.py`

- [ ] **Step 1: Commit verified local source and tests**

Commit only the design, plan, test, `trader.py`, and `PROGRESS.md` preparation entry.

- [ ] **Step 2: Snapshot server state**

Record service state, code/config hashes, positions, trades count, and signal-event count. Create a timestamped backup containing `trader.py`, `demo_bot_config.json`, `positions_<uid>.json`, `trades_<uid>.jsonl`, and `signal_events_0.jsonl`.

- [ ] **Step 3: Deploy code only**

Upload `trader.py`; do not overwrite configuration, positions, trades, signal events, databases, or API credentials.

- [ ] **Step 4: Verify server**

Compile `trader.py`, restart `macd-bot`, confirm both services active, verify local/server SHA-256 equality, verify config and state continuity, confirm a new scan cycle, and run read-only exchange-info probes for failed-symbol market limits.

- [ ] **Step 5: Record deployment**

Update and commit `PROGRESS.md` with change summary, deployed file, backup path, hashes, restart result, continuity checks, and limitations. Do not claim a live fill until a new valid signal actually fills.
