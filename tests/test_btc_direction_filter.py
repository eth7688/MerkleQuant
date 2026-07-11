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

    def test_blocks_short_when_btc_1h_and_4h_are_bullish(self):
        ok, reason = self.bot._btc_direction_filter("SHORT", {
            "btc_1h_overall": "strong_bull",
            "btc_4h_overall": "bull_bias",
        })

        self.assertFalse(ok)
        self.assertEqual(reason, "btc_bull_blocks_short")

    def test_blocks_long_when_btc_1h_and_4h_are_bearish(self):
        ok, reason = self.bot._btc_direction_filter("LONG", {
            "btc_1h_overall": "bear_bias",
            "btc_4h_overall": "strong_bear",
        })

        self.assertFalse(ok)
        self.assertEqual(reason, "btc_bear_blocks_long")

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


if __name__ == "__main__":
    unittest.main()
