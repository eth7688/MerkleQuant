# 0.5R Half-Risk Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional `half_risk_trigger_r` stage that moves a position stop from `-1R` to `-0.5R` after post-entry MFE reaches the configured trigger, while preserving every later exit stage.

**Architecture:** Extend the pure replay exit state machine and the live `SqueezeBreakoutBot.check_exit` state machine with the same one-way half-risk stage. Persist only the stage marker in addition to the already-persisted `current_sl` and MFE, expose one numeric setting in both existing configuration screens, and explicitly enable `0.5` only in the demo-engine runtime configuration during deployment.

**Tech Stack:** Python 3.12, dataclasses, Flask, native HTML/JavaScript, `unittest`, SQLite/JSON-backed existing configuration, systemd deployment.

## Global Constraints

- `initial_risk_per_unit = abs(fill_entry_price - initial_sl)` remains immutable for the full position lifetime.
- A long stop moves to `entry - 0.5R`; a short stop moves to `entry + 0.5R`.
- The new stop remains on the losing side of entry and does not lock floating profit.
- `half_risk_trigger_r <= 0` disables the feature.
- Existing `0.8R` breakeven, `1.2R` defense, partial exits, and trend-runner rules remain unchanged.
- Stop prices may tighten only; they may never loosen.
- Post-entry MFE may trigger the rule; pre-entry candles may not.
- Missing fields in existing ordinary-user configurations must preserve their prior behavior by defaulting to disabled.
- No new dependency, framework, route, or database migration is allowed.
- Local files and tests must be updated before any server deployment.
- Server deployment requires a fresh backup and explicit user approval.

---

### Task 1: Extend the deterministic replay exit state machine

**Files:**
- Modify: `strategy_core.py:59-82`
- Modify: `strategy_core.py:196-250`
- Modify: `backtest/cli.py:148-159`
- Modify: `backtest/cli.py:243-254`
- Test: `tests/test_strategy_core.py:124-147`

**Interfaces:**
- Consumes: `PositionState.mfe_r`, immutable `PositionState.initial_stop`, and existing `ExitRules.early_protect_r`.
- Produces: `ExitRules.half_risk_trigger_r: float`, `PositionState.half_risk_protected: bool`, and `ExitDecision(reason="half_risk_protect")`.

- [ ] **Step 1: Write failing state-machine tests**

Add these methods to `StrategyCoreTest` in `tests/test_strategy_core.py`:

```python
    def test_half_risk_stage_is_symmetric_and_does_not_lock_profit(self):
        rules = ExitRules(half_risk_trigger_r=0.5)

        long_pos = PositionState("LONGUSDT", "LONG", 100.0, 90.0, 1.0)
        long_pos, long_events = advance_position(
            long_pos,
            {"ot": 1, "o": 100.0, "h": 105.0, "l": 100.0, "c": 104.0},
            rules,
        )
        self.assertEqual([(event.reason, event.price) for event in long_events], [("half_risk_protect", 95.0)])
        self.assertTrue(long_pos.half_risk_protected)
        self.assertFalse(long_pos.early_protected)

        short_pos = PositionState("SHORTUSDT", "SHORT", 100.0, 110.0, 1.0)
        short_pos, short_events = advance_position(
            short_pos,
            {"ot": 2, "o": 100.0, "h": 100.0, "l": 95.0, "c": 96.0},
            rules,
        )
        self.assertEqual([(event.reason, event.price) for event in short_events], [("half_risk_protect", 105.0)])
        self.assertTrue(short_pos.half_risk_protected)
        self.assertFalse(short_pos.early_protected)

    def test_half_risk_stage_is_disabled_below_or_at_zero(self):
        for trigger in (0.0, -0.5):
            position = PositionState("TESTUSDT", "LONG", 100.0, 90.0, 1.0)
            position, events = advance_position(
                position,
                {"ot": 1, "o": 100.0, "h": 106.0, "l": 100.0, "c": 105.0},
                ExitRules(half_risk_trigger_r=trigger),
            )
            self.assertEqual(events, [])
            self.assertEqual(position.current_stop, 90.0)

    def test_breakeven_stage_wins_when_one_bar_crosses_both_thresholds(self):
        position = PositionState("TESTUSDT", "LONG", 100.0, 90.0, 1.0)
        position, events = advance_position(
            position,
            {"ot": 1, "o": 100.0, "h": 108.0, "l": 100.0, "c": 107.0},
            ExitRules(half_risk_trigger_r=0.5, early_protect_r=0.8, early_lock_r=0.0),
        )
        self.assertEqual([(event.reason, event.price) for event in events], [("early_protect", 100.0)])
        self.assertTrue(position.half_risk_protected)
        self.assertTrue(position.early_protected)
```

- [ ] **Step 2: Run the new tests and verify that they fail**

Run:

```powershell
python -m unittest tests.test_strategy_core.StrategyCoreTest.test_half_risk_stage_is_symmetric_and_does_not_lock_profit tests.test_strategy_core.StrategyCoreTest.test_half_risk_stage_is_disabled_below_or_at_zero tests.test_strategy_core.StrategyCoreTest.test_breakeven_stage_wins_when_one_bar_crosses_both_thresholds -v
```

Expected: FAIL because `ExitRules` does not accept `half_risk_trigger_r` and `PositionState` has no `half_risk_protected` field.

- [ ] **Step 3: Add the replay rule and stage**

Extend `ExitRules` and `PositionState` in `strategy_core.py`:

```python
@dataclass(frozen=True)
class ExitRules:
    half_risk_trigger_r: float = 0.0
    early_protect_r: float = 0.8
    early_lock_r: float = 0.0
    tier1_r: float = 1.2
    tier1_lock_r: float = 0.5
    tier2_r: float = 1.7
    tier2_fraction: float = 0.5


@dataclass
class PositionState:
    symbol: str
    direction: str
    entry: float
    initial_stop: float
    quantity: float
    current_stop: float | None = None
    remaining_qty: float | None = None
    mfe_r: float = 0.0
    mae_r: float = 0.0
    half_risk_protected: bool = False
    early_protected: bool = False
    tier1_done: bool = False
    tier2_done: bool = False
```

Insert this immediately before the current early-protection block in `advance_position`:

```python
    half_risk_enabled = rules.half_risk_trigger_r > 0
    early_stage_reached = position.mfe_r >= rules.early_protect_r
    if (
        half_risk_enabled
        and not position.half_risk_protected
        and position.mfe_r >= rules.half_risk_trigger_r
        and not early_stage_reached
    ):
        candidate = position.entry - sign * 0.5 * risk
        position.current_stop = (
            max(position.current_stop, candidate)
            if sign > 0
            else min(position.current_stop, candidate)
        )
        position.half_risk_protected = True
        events.append(
            ExitDecision("move_stop", position.current_stop, 0.0, "half_risk_protect", timestamp)
        )

    if not position.early_protected and early_stage_reached:
        candidate = position.entry + sign * rules.early_lock_r * risk
        position.current_stop = max(position.current_stop, candidate) if sign > 0 else min(position.current_stop, candidate)
        position.half_risk_protected = True
        position.early_protected = True
        events.append(ExitDecision("move_stop", position.current_stop, 0.0, "early_protect", timestamp))
```

Replace the original early-protection block rather than leaving both copies.

- [ ] **Step 4: Pass the new rule through both replay CLI paths**

Add the first argument to both `ExitRules(...)` constructions in `backtest/cli.py`:

```python
            half_risk_trigger_r=float(rules.get("half_risk_trigger_r", 0.0)),
```

Keep the replay default at `0.0` so historical baselines do not silently change.

- [ ] **Step 5: Run the focused and existing state-machine tests**

Run:

```powershell
python -m unittest tests.test_strategy_core -v
```

Expected: all `tests.test_strategy_core` tests PASS, including adverse-first same-bar stop handling.

- [ ] **Step 6: Commit the replay state-machine change**

```powershell
git add strategy_core.py backtest/cli.py tests/test_strategy_core.py
git commit -m "feat: model 0.5R half-risk protection"
```

---

### Task 2: Add the live half-risk stage with persistence and retry safety

**Files:**
- Modify: `trader.py:198-205`
- Modify: `trader.py:1055-1082`
- Modify: `trader.py:4958-5001`
- Modify: `trader.py:5022-5051`
- Modify: `trader.py:5195-5221`
- Modify: `trader.py:6744-6955`
- Create: `tests/test_half_risk_protection.py`
- Modify: `tests/test_binance_kline_routing.py:142-189`

**Interfaces:**
- Consumes: `TradeConfig.half_risk_trigger_r`, `Position.initial_sl`, `Position.max_favorable_r`, `Position.current_sl`, and the existing client `stop_order(...)`.
- Produces: persisted `Position.half_risk_protected`, `position_protect` events with `reason="half_risk_protect"`, and a one-way stop upgrade to `-0.5R`.

- [ ] **Step 1: Create failing live-engine tests**

Create `tests/test_half_risk_protection.py` with:

```python
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import Position, SqueezeBreakoutBot, TradeConfig


def make_frame(entry_time, direction, favorable_r):
    risk = 10.0
    rows = 30
    first_open = entry_time - timedelta(minutes=30 * 10)
    highs = [100.0] * rows
    lows = [100.0] * rows
    if direction == "LONG":
        highs[-1] = 100.0 + favorable_r * risk
        lows[-1] = 100.0
        close = 100.0 + favorable_r * risk
    else:
        highs[-1] = 100.0
        lows[-1] = 100.0 - favorable_r * risk
        close = 100.0 - favorable_r * risk
    return pd.DataFrame({
        "ot": [int((first_open + timedelta(minutes=30 * i)).timestamp() * 1000) for i in range(rows)],
        "o": [100.0] * rows,
        "h": highs,
        "l": lows,
        "c": [100.0] * (rows - 1) + [close],
        "v": [1.0] * rows,
    })


def make_position(direction):
    initial_sl = 90.0 if direction == "LONG" else 110.0
    return Position(
        symbol=f"{direction}USDT",
        direction=direction,
        entry_price=100.0,
        entry_time=datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc),
        quantity=1.0,
        sl_price=initial_sl,
        current_sl=initial_sl,
        risk_usdt=10.0,
        signal_score=100.0,
        initial_sl=initial_sl,
        initial_band_hi=101.0,
        initial_band_lo=99.0,
        source_interval="30m",
    )


class FailedStopClient:
    def get_positions(self):
        return []

    def cancel_all_orders(self, symbol):
        return {}

    def stop_order(self, symbol, side, stop_price, quantity, tracking_no=""):
        return None


class HalfRiskProtectionTest(unittest.TestCase):
    def make_bot(self, trigger=0.5):
        return SqueezeBreakoutBot(TradeConfig(
            mode="paper",
            enabled=False,
            enable_time_stop=False,
            half_risk_trigger_r=trigger,
            early_protect_r=0.8,
            early_protect_lock_r=0.0,
            tier1_defense_r=1.2,
            tier2_partial_r=2.0,
            use_atr_trail=False,
        ))

    def test_long_and_short_move_only_to_half_loss(self):
        for direction, expected in (("LONG", 95.0), ("SHORT", 105.0)):
            bot = self.make_bot()
            position = make_position(direction)
            frame = make_frame(position.entry_time, direction, 0.5)

            with patch("trader.fetch_klines", return_value=frame):
                self.assertIsNone(bot.check_exit(position))

            self.assertEqual(position.current_sl, expected)
            self.assertTrue(position.half_risk_protected)
            self.assertFalse(position.breakeven_triggered)

    def test_disabled_and_sub_boundary_values_do_not_move_stop(self):
        cases = ((0.0, 0.7), (0.5, 0.49))
        for trigger, favorable_r in cases:
            bot = self.make_bot(trigger)
            position = make_position("LONG")
            frame = make_frame(position.entry_time, "LONG", favorable_r)

            with patch("trader.fetch_klines", return_value=frame):
                bot.check_exit(position)

            self.assertEqual(position.current_sl, 90.0)
            self.assertFalse(position.half_risk_protected)

    def test_existing_tighter_stop_never_moves_back(self):
        bot = self.make_bot()
        position = make_position("LONG")
        position.current_sl = 98.0

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
            bot.check_exit(position)

        self.assertEqual(position.current_sl, 98.0)
        self.assertTrue(position.half_risk_protected)

    def test_stop_order_failure_keeps_old_state_for_retry(self):
        bot = self.make_bot()
        bot.client = FailedStopClient()
        position = make_position("LONG")

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
            bot.check_exit(position)

        self.assertEqual(position.current_sl, 90.0)
        self.assertFalse(position.half_risk_protected)

    def test_reversed_price_does_not_submit_a_stop_on_the_wrong_side(self):
        bot = self.make_bot()
        position = make_position("LONG")
        frame = make_frame(position.entry_time, "LONG", 0.5)
        frame.loc[frame.index[-1], "c"] = 94.0

        with patch("trader.fetch_klines", return_value=frame):
            bot.check_exit(position)

        self.assertEqual(position.current_sl, 90.0)
        self.assertFalse(position.half_risk_protected)

    def test_persisted_mfe_reapplies_half_risk_after_restart(self):
        bot = self.make_bot()
        position = make_position("LONG")
        position.max_favorable_r = 0.6

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.1)):
            bot.check_exit(position)

        self.assertEqual(position.current_sl, 95.0)
        self.assertTrue(position.half_risk_protected)

    def test_half_risk_state_survives_position_reload(self):
        bot = self.make_bot()
        position = make_position("LONG")
        position.current_sl = 95.0
        position.half_risk_protected = True
        bot.positions = [position]

        with TemporaryDirectory() as directory:
            bot._positions_path = str(Path(directory) / "positions.json")
            bot._save_positions()
            restored = bot._load_positions()

        self.assertEqual(restored[0].current_sl, 95.0)
        self.assertTrue(restored[0].half_risk_protected)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Strengthen the existing pre-entry-MFE regression**

In `test_new_position_does_not_use_pre_entry_mark_price_extrema` in `tests/test_binance_kline_routing.py`, pass:

```python
            half_risk_trigger_r=0.5,
```

Then add:

```python
        self.assertFalse(position.half_risk_protected)
        self.assertEqual(position.current_sl, position.initial_sl)
```

- [ ] **Step 3: Run the live tests and verify that they fail**

Run:

```powershell
python -m unittest tests.test_half_risk_protection tests.test_binance_kline_routing.BinanceKlineRoutingTest.test_new_position_does_not_use_pre_entry_mark_price_extrema -v
```

Expected: FAIL because `TradeConfig` and `Position` do not yet define the new fields and `check_exit` does not apply the stage.

- [ ] **Step 4: Add config and persistent position fields**

In `TradeConfig` in `trader.py`, insert before `enable_early_protect`:

```python
    half_risk_trigger_r: float = 0.0  # >0启用: 达阈值后把初始1R风险收窄到0.5R
```

In `Position`, insert before `breakeven_triggered`:

```python
    half_risk_protected: bool = False
```

Add this item in `_save_positions`:

```python
                    "half_risk_protected": bool(getattr(p, "half_risk_protected", False)),
```

Restore it in `_load_positions`:

```python
                pos.half_risk_protected = bool(d.get("half_risk_protected", False))
```

Copy it from the matched local position in `_sync_positions`:

```python
                            pos.half_risk_protected = bool(
                                getattr(matched_local, "half_risk_protected", False)
                            ) if matched_local else False
```

- [ ] **Step 5: Implement the live half-risk decision before breakeven**

Immediately after MFE/MAE persistence and before the existing early-protection block in `check_exit`, compute the shared protection values:

```python
        half_trigger_r = max(
            0.0,
            float(getattr(self.cfg, "half_risk_trigger_r", 0.0) or 0.0),
        )
        early_trigger_r = max(
            0.1,
            float(getattr(self.cfg, "early_protect_r", 0.8) or 0.8),
        )
        protect_r = max(
            float(r_multiple),
            float(favorable_r),
            float(getattr(pos, "max_favorable_r", 0.0) or 0.0),
        )

        if (
            half_trigger_r > 0
            and not pos.partial_tp_triggered
            and initial_risk > 0
            and pos.quantity > 0
            and protect_r >= half_trigger_r
            and protect_r < early_trigger_r
        ):
            desired_sl = (
                pos.entry_price - initial_risk * 0.5
                if pos.direction == "LONG"
                else pos.entry_price + initial_risk * 0.5
            )
            should_move = (
                (pos.direction == "LONG" and desired_sl > pos.current_sl)
                or (pos.direction == "SHORT" and desired_sl < pos.current_sl)
            )
            already_tighter = (
                (pos.direction == "LONG" and pos.current_sl >= desired_sl)
                or (pos.direction == "SHORT" and pos.current_sl <= desired_sl)
            )
            valid_price_side = (
                (pos.direction == "LONG" and desired_sl < current_price)
                or (pos.direction == "SHORT" and desired_sl > current_price)
            )
            if should_move and not valid_price_side:
                self._log.warning(
                    f"半损保护跳过: {symbol} action=invalid_price_side "
                    f"current={current_price:.4f} target={desired_sl:.4f}"
                )
            elif should_move:
                old_sl = pos.current_sl
                applied = self.client is None
                stop_result = None
                if self.client is not None:
                    self.client.cancel_all_orders(symbol)
                    sl_side = "SELL" if pos.direction == "LONG" else "BUY"
                    stop_result = self.client.stop_order(
                        symbol,
                        sl_side,
                        round(desired_sl, 8),
                        round(pos.quantity, 8),
                        tracking_no=pos.tracking_no,
                    )
                    if bool(getattr(self.cfg, "testnet", False)):
                        applied = True
                    elif str(getattr(self.cfg, "exchange", "")).lower() == "bitget":
                        applied = (
                            isinstance(stop_result, dict)
                            and (
                                stop_result.get("code") == "00000"
                                or bool(stop_result.get("orderId"))
                            )
                        )
                    else:
                        applied = bool(stop_result)
                if applied:
                    pos.current_sl = desired_sl
                    pos.half_risk_protected = True
                    self._log.info(
                        f"半损保护: {symbol} ({inv}) MFE={protect_r:.2f}R>={half_trigger_r:.2f}R "
                        f"SL {old_sl:.4f}->{pos.current_sl:.4f} 最大亏损收窄至0.50R"
                    )
                    self._append_signal_event("position_protect", symbol, {
                        "symbol": symbol,
                        "direction": pos.direction,
                        "interval": inv,
                        "reason": "half_risk_protect",
                        "entry": round(float(pos.entry_price), 8),
                        "initial_sl": round(float(pos.initial_sl), 8),
                        "current_price": round(float(current_price), 8),
                        "old_sl": round(float(old_sl), 8),
                        "new_sl": round(float(pos.current_sl), 8),
                        "r": round(float(r_multiple), 4),
                        "protect_r": round(float(protect_r), 4),
                        "mfe_r": round(float(getattr(pos, "max_favorable_r", 0.0)), 4),
                        "mae_r": round(float(getattr(pos, "max_adverse_r", 0.0)), 4),
                        "trigger_r": half_trigger_r,
                        "lock_r": -0.5,
                        "action": "half_risk_applied",
                        "quantity": round(float(pos.quantity), 8),
                        "signal_key": getattr(pos, "signal_key", ""),
                    })
                    self._save_positions()
                else:
                    self._log.error(
                        f"半损保护待重试: {symbol} action=retry_pending SL保持{old_sl:.4f}, "
                        f"目标{desired_sl:.4f}, stop_result={stop_result}"
                    )
            elif already_tighter and not pos.half_risk_protected:
                pos.half_risk_protected = True
                self._log.info(
                    f"半损保护状态恢复: {symbol} action=already_tighter "
                    f"current_sl={pos.current_sl:.4f} target={desired_sl:.4f}"
                )
                self._save_positions()
```

Reuse the already-computed `early_trigger_r` and `protect_r` in the existing early-protection block by removing its duplicate assignments. When the existing early stage succeeds or discovers an already-tighter stop, also set:

```python
                    pos.half_risk_protected = True
```

Do not set `breakeven_triggered` or `breakeven_cooldown` in the half-risk stage. Those fields retain their current breakeven-only meaning.

- [ ] **Step 6: Preserve protection-aware time-stop fallback behavior**

In the early local time-stop fallback near the start of `check_exit`, replace the single early-protection switch with:

```python
                protection_on = (
                    bool(getattr(self.cfg, "enable_early_protect", True))
                    or float(getattr(self.cfg, "half_risk_trigger_r", 0.0) or 0.0) > 0
                )
                if quick_r is None and not protection_on:
```

This prevents a missing local price from bypassing MFE recovery while either protection stage is enabled. Leave the later time-stop decision unchanged; half-risk protection alone must not masquerade as breakeven.

- [ ] **Step 7: Run focused live-engine tests**

Run:

```powershell
python -m unittest tests.test_half_risk_protection tests.test_binance_kline_routing -v
```

Expected: all tests PASS. Confirm that the failure test leaves both `current_sl` and `half_risk_protected` unchanged.

- [ ] **Step 8: Commit the live engine change**

```powershell
git add trader.py tests/test_half_risk_protection.py tests/test_binance_kline_routing.py
git commit -m "feat: add live 0.5R half-risk stop"
```

---

### Task 3: Expose the single numeric parameter without changing old configs

**Files:**
- Modify: `admin_server.py:440-460`
- Modify: `admin_server.py:569-580`
- Modify: `web_ui.py:1836-1848`
- Modify: `web_ui.py:2510-2524`
- Modify: `tests/test_predicta_config.py`

**Interfaces:**
- Consumes: `TradeConfig.half_risk_trigger_r`.
- Produces: admin input `deHalfRiskR`, user input `cfg_half_risk_r`, and JSON key `half_risk_trigger_r`.

- [ ] **Step 1: Write a failing configuration-surface test**

Add this method to `PredictaConfigTest` in `tests/test_predicta_config.py`:

```python
    def test_half_risk_defaults_off_and_both_config_screens_expose_one_parameter(self):
        self.assertEqual(TradeConfig().half_risk_trigger_r, 0.0)

        admin_source = (ROOT / "admin_server.py").read_text(encoding="utf-8")
        self.assertIn('id="deHalfRiskR"', admin_source)
        self.assertIn(
            "half_risk_trigger_r: parseFloat(document.getElementById('deHalfRiskR').value)||0",
            admin_source,
        )
        self.assertIn("(cfg.half_risk_trigger_r??0)", admin_source)

        user_source = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn('id="cfg_half_risk_r"', user_source)
        self.assertIn(
            "half_risk_trigger_r: parseFloat(document.getElementById('cfg_half_risk_r').value)||0",
            user_source,
        )
        self.assertIn("(cfg.half_risk_trigger_r??0)", user_source)
```

- [ ] **Step 2: Run the configuration test and verify that it fails**

Run:

```powershell
python -m unittest tests.test_predicta_config.PredictaConfigTest.test_half_risk_defaults_off_and_both_config_screens_expose_one_parameter -v
```

Expected: FAIL because neither screen contains the new input.

- [ ] **Step 3: Add the demo-admin field and save mapping**

Add this item in `saveDemoConfig()` in `admin_server.py`:

```javascript
    half_risk_trigger_r: parseFloat(document.getElementById('deHalfRiskR').value)||0,
```

Add this field immediately before the existing “提前保护” field:

```javascript
    h+='<div class="field"><label>0.5R半损保护 (0=关闭)</label><input type="number" id="deHalfRiskR" value="'+(cfg.half_risk_trigger_r??0)+'" min="0" max="0.79" step="0.1"></div>';
```

The field controls only the trigger. The destination remains fixed at `-0.5R`.

- [ ] **Step 4: Add the user-config field and save mapping**

Add this item in the existing user configuration object in `web_ui.py`:

```javascript
    half_risk_trigger_r: parseFloat(document.getElementById('cfg_half_risk_r').value)||0,
```

Add this field immediately before the existing “提前保护” field:

```javascript
    h+='<div class="t-field"><label>0.5R半损保护<span class="tip">!<span class="tip-text">达到设置的R后，把最大亏损从1R降到0.5R<br>0=关闭，不锁浮盈</span></span></label><input type="number" id="cfg_half_risk_r" value="'+(cfg.half_risk_trigger_r??0)+'" min="0" max="0.79" step="0.1"></div>';
```

Do not change any element ID, class, route, or save mapping outside these two insertions.

- [ ] **Step 5: Run configuration and source-surface tests**

Run:

```powershell
python -m unittest tests.test_predicta_config -v
```

Expected: all `tests.test_predicta_config` tests PASS.

- [ ] **Step 6: Commit the configuration surface**

```powershell
git add admin_server.py web_ui.py tests/test_predicta_config.py
git commit -m "feat: expose half-risk trigger setting"
```

---

### Task 4: Verify locally and prepare an approval-gated demo deployment

**Files:**
- Modify after successful deployment only: `PROGRESS.md`
- Runtime-only server update after approval: `<deploy-dir>/demo_bot_config.json`
- Deploy after approval: `<deploy-dir>/trader.py`
- Deploy after approval: `<deploy-dir>/strategy_core.py`
- Deploy after approval: `<deploy-dir>/backtest/cli.py`
- Deploy after approval: `<deploy-dir>/admin_server.py`
- Deploy after approval: `<deploy-dir>/web_ui.py`

**Interfaces:**
- Consumes: all committed code and tests from Tasks 1-3.
- Produces: verified local build, an approval checkpoint, then a backed-up and verified demo deployment with `half_risk_trigger_r=0.5`.

- [ ] **Step 1: Run the focused regression suite**

Run:

```powershell
python -m unittest tests.test_strategy_core tests.test_half_risk_protection tests.test_binance_kline_routing tests.test_predicta_config -v
```

Expected: all focused tests PASS.

- [ ] **Step 2: Run the full automated test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests PASS with no errors or failures.

- [ ] **Step 3: Run compilation and whitespace checks**

Run:

```powershell
python -m py_compile trader.py strategy_core.py backtest/cli.py admin_server.py web_ui.py tests/test_half_risk_protection.py
git diff --check
git status --short
```

Expected: compilation exits `0`, `git diff --check` prints nothing, and the worktree contains no uncommitted implementation files.

- [ ] **Step 4: Stop and obtain explicit deployment approval**

Report the exact local test counts, changed files, current demo positions, and the proposed server backup path. Do not connect to or modify the server until the user explicitly approves deployment.

- [ ] **Step 5: Back up server code, config, positions, and services after approval**

On the server, use a timestamped directory and copy—not move—the current files:

```bash
cd <deploy-dir>
stamp=$(date +%Y%m%d_%H%M%S)
backup="<deploy-dir>/backups/half_risk_${stamp}"
mkdir -p "$backup"
cp trader.py strategy_core.py admin_server.py web_ui.py demo_bot_config.json "$backup"/
test -f backtest/cli.py && mkdir -p "$backup/backtest" && cp backtest/cli.py "$backup/backtest"/
test -f demo_positions.json && cp demo_positions.json "$backup"/
systemctl is-active macd-bot macd-admin
```

Expected: both services report `active`, and the backup contains every existing source/config/state file named above.

- [ ] **Step 6: Enable the demo config without overwriting API credentials**

Patch only the new key in the server JSON using Python:

```bash
cd <deploy-dir>
python3 - <<'PY'
import json
from pathlib import Path

path = Path("demo_bot_config.json")
data = json.loads(path.read_text(encoding="utf-8"))
data["half_risk_trigger_r"] = 0.5
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print({"half_risk_trigger_r": data["half_risk_trigger_r"], "keys": len(data)})
PY
```

Expected: output reports `half_risk_trigger_r: 0.5`; all existing keys, including exchange and credential fields, remain present.

- [ ] **Step 7: Upload only verified source files and restart services**

Upload the committed versions of:

```text
trader.py
strategy_core.py
backtest/cli.py
admin_server.py
web_ui.py
```

Then run:

```bash
cd <deploy-dir>
python3 -m py_compile trader.py strategy_core.py backtest/cli.py admin_server.py web_ui.py
systemctl restart macd-bot
systemctl restart macd-admin
systemctl is-active macd-bot macd-admin
```

Expected: compilation exits `0`; both services report `active`.

- [ ] **Step 8: Verify configuration, position continuity, and logs**

Run read-only checks:

```bash
cd <deploy-dir>
python3 - <<'PY'
import json
from pathlib import Path

cfg = json.loads(Path("demo_bot_config.json").read_text(encoding="utf-8"))
print({"half_risk_trigger_r": cfg.get("half_risk_trigger_r")})
for name in ("demo_positions.json", "positions_demo.json", "positions.json"):
    path = Path(name)
    if path.exists():
        rows = json.loads(path.read_text(encoding="utf-8"))
        print(name, len(rows), [
            {
                "symbol": row.get("symbol"),
                "current_sl": row.get("current_sl"),
                "half_risk_protected": row.get("half_risk_protected"),
                "active_stop_id": row.get("active_stop_id"),
            }
            for row in rows
        ])
PY
journalctl -u macd-bot -n 120 --no-pager
journalctl -u macd-admin -n 60 --no-pager
```

Expected:

- configuration prints `0.5`;
- pre-existing positions remain present with their prior `current_sl`;
- no duplicate active stop IDs appear;
- no Python traceback or repeated stop-order error appears;
- positions whose post-entry MFE already reached `0.5R` either receive `half_risk_protect` once or already have a tighter stop.

- [ ] **Step 9: Record the verified deployment**

Append a concrete entry to `PROGRESS.md` containing:

```markdown
## 2026-07-23 — 0.5R 半损保护部署

- 修改：新增 `half_risk_trigger_r`；达到配置阈值后把止损从 `-1R` 收紧到 `-0.5R`，不锁浮盈。
- 保持不变：0.8R 保本、1.2R 防守、分批止盈、趋势尾仓、开仓链路。
- 部署：列出实际上传文件和服务器目标目录。
- 备份：记录实际时间戳备份目录。
- 配置：演示引擎 `half_risk_trigger_r=0.5`；普通用户缺省仍为 `0.0`。
- 验证：记录本地测试数量、编译结果、systemd 状态、持仓连续性和止损单去重结果。
```

Commit only after replacing each descriptive phrase with the actual observed result:

```powershell
git add PROGRESS.md
git commit -m "docs: record half-risk protection deployment"
```
