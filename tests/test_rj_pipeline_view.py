import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import SqueezeBreakoutBot, TradeConfig


class RjPipelineViewTest(unittest.TestCase):
    def test_setup_pool_rows_expose_frontend_stage_fields(self):
        bot = SqueezeBreakoutBot(TradeConfig(mode="paper", enabled=False, exchange="bitget"))
        bot._rj_setup_pool = {
            "TESTUSDT:LONG:30m": {
                "symbol": "TESTUSDT",
                "direction": "LONG",
                "source_interval": "30m",
                "score": 72.5,
                "price": 1.0,
                "rj_setup_live_price": 1.099,
                "rj_setup_trigger_price": 1.10,
                "rj_only_stop_price": 0.94,
                "rj_only_hist_win_rate": 66.7,
                "rj_only_hist_samples": 18,
                "rj_setup_first_seen_ts": 1000.0,
                "rj_setup_expires_at_ts": 4600.0,
                "rj_setup_confirm_mode": "kline_current",
            }
        }

        rows = bot._rj_setup_pool_rows(limit=5, now_ts=1300.0)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "TESTUSDT")
        self.assertEqual(row["stage"], "near")
        self.assertEqual(row["stage_label"], "NEAR")
        self.assertAlmostEqual(row["distance_pct"], 0.0909, places=4)
        self.assertEqual(row["hist_samples"], 18)
        self.assertEqual(row["expires_in_sec"], 3300)

    def test_setup_pool_rows_collapse_same_symbol_direction_for_frontend(self):
        bot = SqueezeBreakoutBot(TradeConfig(mode="paper", enabled=False, exchange="bitget"))
        base = {
            "symbol": "DUPUSDT",
            "direction": "SHORT",
            "source_interval": "30m",
            "price": 10.0,
            "rj_setup_live_price": 10.0,
            "rj_only_hist_samples": 20,
            "rj_setup_first_seen_ts": 1000.0,
            "rj_setup_expires_at_ts": 4600.0,
        }
        bot._rj_setup_pool = {
            "a": {**base, "score": 70, "rj_setup_trigger_price": 9.9, "rj_only_stop_price": 10.4},
            "b": {**base, "score": 75, "rj_setup_trigger_price": 9.95, "rj_only_stop_price": 10.3},
        }

        rows = bot._rj_setup_pool_rows(limit=10, now_ts=1300.0)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "DUPUSDT")
        self.assertEqual(rows[0]["score"], 75)
        self.assertAlmostEqual(rows[0]["distance_pct"], 0.5025, places=4)


if __name__ == "__main__":
    unittest.main()
