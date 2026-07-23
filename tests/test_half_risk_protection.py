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
            ("LONG", 90.0, 95.0),
            ("SHORT", 110.0, 105.0),
            ("LONG", 98.0, 98.0),
            ("SHORT", 102.0, 102.0),
        )
        for direction, saved_sl, expected in cases:
            with self.subTest(direction=direction, saved_sl=saved_sl), TemporaryDirectory() as directory:
                path = str(Path(directory) / "positions.json")
                writer = self.make_bot()
                stale = make_position(direction)
                stale.current_sl = saved_sl
                stale.max_favorable_r = 0.6
                writer.positions = [stale]
                writer._positions_path = path
                writer._save_positions()

                restarted = self.make_bot()
                restarted._positions_path = path
                restarted.client = RestartSyncClient(direction)
                restarted._sync_positions()

                self.assertEqual(restarted.positions[0].current_sl, expected)
                self.assertTrue(restarted.positions[0].half_risk_protected)
                self.assertGreater(len(restarted.client.stop_calls), 0)
                self.assertTrue(all(call[2] == expected for call in restarted.client.stop_calls))
                reloaded = restarted._load_positions()
                self.assertEqual(reloaded[0].current_sl, expected)
                self.assertTrue(reloaded[0].half_risk_protected)


if __name__ == "__main__":
    unittest.main()
