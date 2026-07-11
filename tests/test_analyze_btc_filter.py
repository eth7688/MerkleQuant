import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_btc_filter import dedupe_events, evaluate_path, trade_would_be_blocked


class AnalyzeBtcFilterTest(unittest.TestCase):
    def test_dedupe_keeps_latest_event_for_same_setup(self):
        events = [
            {"time": "2026-07-10T16:00:00+00:00", "symbol": "CATIUSDT", "direction": "SHORT", "rj_only_key_time": 1},
            {"time": "2026-07-10T16:10:00+00:00", "symbol": "CATIUSDT", "direction": "SHORT", "rj_only_key_time": 1},
        ]

        result = dedupe_events(events)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["time"], "2026-07-10T16:10:00+00:00")

    def test_short_path_records_plus_one_r_before_stop(self):
        event = {"direction": "SHORT", "entry": 100.0, "sl": 102.0, "target_r": 0.5}
        candles = [
            [1, 100.0, 100.5, 99.5, 100.0],
            [2, 100.0, 100.2, 97.8, 98.0],
            [3, 98.0, 102.1, 97.5, 101.0],
        ]

        result = evaluate_path(event, candles)

        self.assertEqual(result["first_event"], "plus_1R")
        self.assertAlmostEqual(result["mfe_r"], 1.25)
        self.assertAlmostEqual(result["mae_r"], 1.05)
        self.assertAlmostEqual(result["final_r"], -0.5)
        self.assertTrue(result["target_reached"])

    def test_trade_filter_requires_both_btc_timeframes(self):
        blocked = {
            "direction": "SHORT",
            "btc_1h_overall": "strong_bull",
            "btc_4h_overall": "bull_bias",
        }
        mixed = dict(blocked, btc_4h_overall="neutral")

        self.assertTrue(trade_would_be_blocked(blocked))
        self.assertFalse(trade_would_be_blocked(mixed))


if __name__ == "__main__":
    unittest.main()
