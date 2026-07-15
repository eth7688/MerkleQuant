import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import SqueezeBreakoutBot, TradeConfig, Position, bj_now


def make_position(source_strategy="rj_only"):
    pos = Position(
        symbol="TESTUSDT",
        direction="LONG",
        entry_price=10.0,
        entry_time=bj_now() - timedelta(hours=8),
        quantity=10.0,
        sl_price=9.0,
        current_sl=9.0,
        risk_usdt=10.0,
        signal_score=90.0,
        initial_band_hi=10.2,
        initial_band_lo=9.8,
        source_interval="30m",
        source_strategy=source_strategy,
    )
    pos.initial_sl = 9.0
    pos.signal_key = "RJ|TESTUSDT|LONG|30m" if source_strategy == "rj_only" else "STRUCT|TEST"
    pos.max_favorable_r = 0.7
    return pos


class RjTimeStopTest(unittest.TestCase):
    def make_bot(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        cfg.enable_time_stop = True
        cfg.time_stop_bars = "30m:6"
        cfg.time_stop_min_r = 0.6
        cfg.rj_time_stop_extend_bars = 6
        cfg.rj_time_stop_hard_loss_r = -0.8
        cfg.rj_time_stop_final_min_r = 0.3
        return SqueezeBreakoutBot(cfg)

    def test_rj_only_final_time_stop_no_longer_closes_slow_starter(self):
        bot = self.make_bot()
        pos = make_position("rj_only")

        reason = bot._time_stop_exit_decision(pos, "30m", 12, 0.1, 0.6, "unit")

        self.assertIsNone(reason)

    def test_rj_only_time_stop_still_closes_hard_loss(self):
        bot = self.make_bot()
        pos = make_position("rj_only")

        reason = bot._time_stop_exit_decision(pos, "30m", 12, -0.9, 0.6, "unit")

        self.assertIsNotNone(reason)
        self.assertIn("硬", reason)

    def test_structure_time_stop_still_closes_failed_breakout(self):
        bot = self.make_bot()
        pos = make_position("structure")

        reason = bot._time_stop_exit_decision(pos, "30m", 6, 0.1, 0.6, "unit")

        self.assertIsNotNone(reason)
        self.assertIn("未起爆", reason)

    def test_predicta_position_is_exempt_from_all_time_stops(self):
        bot = self.make_bot()
        pos = make_position("predicta_ewo")

        reason = bot._time_stop_exit_decision(pos, "30m", 100, -5.0, 0.6, "unit")

        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
