import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import SqueezeBreakoutBot, TradeConfig


class BtcDirectionFilterTest(unittest.TestCase):
    def setUp(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget", entry_signal_source="rj_only")
        self.bot = SqueezeBreakoutBot(cfg)

    def test_does_not_block_short_for_ordinary_aligned_bull_bias(self):
        ok, reason = self.bot._btc_direction_filter("SHORT", {
            "btc_1h_overall": "strong_bull",
            "btc_4h_overall": "bull_bias",
        })

        self.assertTrue(ok)
        self.assertEqual(reason, "pass")

    def test_blocks_only_explicit_extreme_bear_stage(self):
        ok, reason = self.bot._btc_direction_filter("LONG", {
            "btc_stage": "mid_bear",
            "btc_direction": "bear",
            "btc_extreme_veto": True,
        })

        self.assertFalse(ok)
        self.assertEqual(reason, "btc_extreme_bear_blocks_long")

    def test_does_not_block_mixed_or_unknown_btc_state(self):
        cases = [
            ("SHORT", {"btc_1h_overall": "strong_bull", "btc_4h_overall": "neutral"}),
            ("LONG", {"btc_1h_overall": "unknown", "btc_4h_overall": "strong_bear"}),
        ]

        for direction, fields in cases:
            with self.subTest(direction=direction, fields=fields):
                ok, reason = self.bot._btc_direction_filter(direction, fields)
                self.assertTrue(ok)
                self.assertEqual(reason, "pass")

    def test_coin_reversal_package_uses_signal_candle_evidence(self):
        good = {
            "rj_sr_near_resistance": True,
            "rj_sr_bear_div": True,
            "rj_volume_filter_pass": True,
        }
        missing_divergence = {**good, "rj_sr_bear_div": False}

        self.assertTrue(self.bot._btc_coin_reversal_pass(good, "SHORT"))
        self.assertFalse(self.bot._btc_coin_reversal_pass(missing_divergence, "SHORT"))


if __name__ == "__main__":
    unittest.main()
