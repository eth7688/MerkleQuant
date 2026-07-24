import json
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
    def __init__(self):
        self.stop_calls = []

    def get_positions(self):
        return []

    def cancel_all_orders(self, symbol):
        return {}

    def stop_order(self, symbol, side, stop_price, quantity, tracking_no=""):
        self.stop_calls.append((symbol, side, stop_price, quantity, tracking_no))
        return None


class SuccessfulTrackingStopClient(FailedStopClient):
    def stop_order(self, symbol, side, stop_price, quantity, tracking_no=""):
        return {}


class RestartSyncClient:
    def __init__(self, direction):
        self.direction = direction
        self.stop_calls = []
        self._active_stop_ids = {}

    def get_positions(self):
        return [{
            "symbol": f"{self.direction}USDT",
            "positionAmt": 1.0 if self.direction == "LONG" else -1.0,
            "entryPrice": 100.0,
            "markPrice": 101.0,
            "unRealizedProfit": 1.0,
            "openTime": "",
        }]

    def stop_order(self, symbol, side, stop_price, quantity, tracking_no=""):
        self.stop_calls.append((symbol, side, stop_price, quantity, tracking_no))
        return {"orderId": f"stop-{len(self.stop_calls)}"}


class SequencedStopClient(FailedStopClient):
    def __init__(self, results, events=None):
        super().__init__()
        self.results = list(results)
        self.events = events

    def cancel_all_orders(self, symbol):
        if self.events is not None:
            self.events.append(("cancel", symbol))
        return {}

    def stop_order(self, symbol, side, stop_price, quantity, tracking_no=""):
        self.stop_calls.append((symbol, side, stop_price, quantity, tracking_no))
        if self.events is not None:
            self.events.append(("stop", stop_price))
        return self.results.pop(0)


class BitgetRollbackClient(FailedStopClient):
    def __init__(self):
        super().__init__()
        self._active_stop_ids = {"LONGUSDT": "old-id"}

    def cancel_all_orders(self, symbol):
        self._active_stop_ids.pop(symbol, None)
        return {"code": "00000"}

    def stop_order(self, symbol, side, stop_price, quantity, tracking_no=""):
        self.stop_calls.append((symbol, side, stop_price, quantity, tracking_no))
        if len(self.stop_calls) == 1:
            return None
        self._active_stop_ids[symbol] = "rollback-id"
        return {"orderId": "rollback-id"}


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

    def test_disabled_early_protection_does_not_suppress_half_risk_stage(self):
        bot = self.make_bot()
        bot.cfg.enable_early_protect = False
        position = make_position("LONG")

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.9)):
            bot.check_exit(position)

        self.assertEqual(position.current_sl, 95.0)
        self.assertTrue(position.half_risk_protected)
        self.assertFalse(position.breakeven_triggered)

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

    def test_failed_new_stop_restores_old_exchange_stop(self):
        bot = self.make_bot()
        bot.client = SequencedStopClient([None, {"orderId": "rollback-1"}])
        position = make_position("LONG")

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
            bot.check_exit(position)

        self.assertEqual([call[2] for call in bot.client.stop_calls], [95.0, 90.0])
        self.assertEqual(position.current_sl, 90.0)
        self.assertFalse(position.half_risk_protected)

    def test_bitget_rollback_persists_and_restores_new_active_stop_id(self):
        bot = self.make_bot()
        bot.cfg.exchange = "bitget"
        bot.client = BitgetRollbackClient()
        position = make_position("LONG")
        bot.positions = [position]

        with TemporaryDirectory() as directory:
            bot._positions_path = str(Path(directory) / "positions.json")
            with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
                bot.check_exit(position)

            with open(bot._positions_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            bot.client._active_stop_ids = {}
            bot._restore_stop_ids()

        self.assertEqual([call[2] for call in bot.client.stop_calls], [95.0, 90.0])
        self.assertEqual(position.current_sl, 90.0)
        self.assertFalse(position.half_risk_protected)
        self.assertEqual(saved[0]["active_stop_id"], "rollback-id")
        self.assertEqual(bot.client._active_stop_ids["LONGUSDT"], "rollback-id")

    def test_failed_new_and_rollback_stops_log_critical_retry(self):
        bot = self.make_bot()
        bot.client = SequencedStopClient([None, None])
        position = make_position("LONG")

        with self.assertLogs(bot._log, level="CRITICAL") as captured:
            with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
                bot.check_exit(position)

        self.assertEqual([call[2] for call in bot.client.stop_calls], [95.0, 90.0])
        self.assertEqual(position.current_sl, 90.0)
        self.assertFalse(position.half_risk_protected)
        self.assertIn("critical", captured.output[0].lower())
        self.assertIn("retry_pending", captured.output[0])

    def test_small_positive_trigger_saves_mfe_before_cancel_replace(self):
        events = []
        bot = self.make_bot(trigger=0.01)
        bot.client = SequencedStopClient([{"orderId": "new-1"}], events)
        position = make_position("LONG")

        def record_save():
            events.append(("save", position.max_favorable_r))

        bot._save_positions = record_save
        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.02)):
            bot.check_exit(position)

        self.assertEqual(events[0][0], "save")
        self.assertGreaterEqual(events[0][1], 0.01)
        self.assertEqual(events[1:], [("cancel", "LONGUSDT"), ("stop", 95.0), ("save", position.max_favorable_r)])

    def test_testnet_uses_internal_stop_when_exchange_returns_none(self):
        bot = self.make_bot()
        bot.cfg.testnet = True
        bot.client = FailedStopClient()
        position = make_position("LONG")

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
            bot.check_exit(position)

        self.assertEqual(len(bot.client.stop_calls), 1)
        self.assertEqual(position.current_sl, 95.0)
        self.assertTrue(position.half_risk_protected)

    def test_bitget_tracking_stop_accepts_non_none_data_as_success(self):
        bot = self.make_bot()
        bot.cfg.exchange = "bitget"
        bot.client = SuccessfulTrackingStopClient()
        position = make_position("LONG")
        position.tracking_no = "tracking-1"

        with patch("trader.fetch_klines", return_value=make_frame(position.entry_time, "LONG", 0.5)):
            bot.check_exit(position)

        self.assertEqual(position.current_sl, 95.0)
        self.assertTrue(position.half_risk_protected)

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

    def test_restart_sync_rebuilds_half_risk_stop_before_any_rehang(self):
        cases = (
            ("LONG", 90.0, 95.0, 10.0),
            ("SHORT", 110.0, 105.0, 10.0),
            ("LONG", 98.0, 98.0, 7.5),
            ("SHORT", 102.0, 102.0, 8.5),
        )
        for direction, saved_sl, expected, persisted_risk in cases:
            with self.subTest(direction=direction, saved_sl=saved_sl), TemporaryDirectory() as directory:
                path = str(Path(directory) / "positions.json")
                writer = self.make_bot()
                stale = make_position(direction)
                stale.current_sl = saved_sl
                stale.risk_usdt = persisted_risk
                stale.max_favorable_r = 0.6
                writer.positions = [stale]
                writer._positions_path = path
                writer._save_positions()

                restarted = self.make_bot()
                restarted._positions_path = path
                restarted.client = RestartSyncClient(direction)
                restarted._sync_positions()

                self.assertEqual(restarted.positions[0].current_sl, expected)
                self.assertEqual(restarted.positions[0].initial_sl, stale.initial_sl)
                self.assertEqual(restarted.positions[0].sl_price, stale.initial_sl)
                self.assertEqual(restarted.positions[0].risk_usdt, persisted_risk)
                self.assertTrue(restarted.positions[0].half_risk_protected)
                self.assertGreater(len(restarted.client.stop_calls), 0)
                self.assertTrue(all(call[2] == expected for call in restarted.client.stop_calls))
                reloaded = restarted._load_positions()
                self.assertEqual(reloaded[0].current_sl, expected)
                self.assertEqual(reloaded[0].risk_usdt, persisted_risk)
                self.assertTrue(reloaded[0].half_risk_protected)


if __name__ == "__main__":
    unittest.main()
