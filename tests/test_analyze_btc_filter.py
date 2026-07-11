import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_btc_filter import build_summary, dedupe_events, evaluate_horizons, evaluate_path, trade_would_be_blocked


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

    def test_trade_filter_uses_stage_veto_or_missing_reversal_package(self):
        blocked = {
            "direction": "SHORT",
            "btc_stage": "early_bull",
            "btc_direction": "bull",
            "btc_extreme_veto": True,
        }
        late = dict(blocked, btc_stage="late_bull", btc_extreme_veto=False)
        ordinary_missing_reversal = dict(blocked, btc_stage="mid_bull", btc_extreme_veto=False)
        ordinary_with_reversal = dict(ordinary_missing_reversal, btc_coin_reversal_pass=True)

        self.assertTrue(trade_would_be_blocked(blocked))
        self.assertFalse(trade_would_be_blocked(late))
        self.assertTrue(trade_would_be_blocked(ordinary_missing_reversal))
        self.assertFalse(trade_would_be_blocked(ordinary_with_reversal))

    def test_evaluate_horizons_reports_6_12_24_bars(self):
        event = {"direction": "LONG", "entry": 100.0, "sl": 99.0}
        candles = []
        for minute in range(24 * 30):
            price = 100.0 + minute / 300.0
            candles.append([minute * 60_000, price, price + 0.1, price - 0.1, price])

        result = evaluate_horizons(event, candles)

        self.assertEqual(sorted(result), ["12", "24", "6"])
        self.assertLess(result["6"]["mfe_r"], result["12"]["mfe_r"])
        self.assertLess(result["12"]["mfe_r"], result["24"]["mfe_r"])

    def test_summary_requires_50_complete_samples(self):
        reviewed = [{"complete": True, "error": "", "first_event": "plus_1R"} for _ in range(49)]

        result = build_summary(reviewed, 50)

        self.assertFalse(result["sample_ready"])
        self.assertEqual(result["status"], "SAMPLE_NOT_READY")


if __name__ == "__main__":
    unittest.main()
