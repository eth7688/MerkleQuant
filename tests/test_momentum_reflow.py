import unittest

import numpy as np
import pandas as pd

from momentum_reflow import add_hourly_indicators, daily_confirmation


def candle_frame(count=80, start=100.0):
    close = np.linspace(start, start + count - 1, count)
    return pd.DataFrame({
        "ot": np.arange(count, dtype=np.int64) * 3_600_000,
        "o": close - 0.4,
        "h": close + 1.0,
        "l": close - 1.0,
        "c": close,
        "v": np.full(count, 100.0),
    })


class HourlyIndicatorTests(unittest.TestCase):
    def test_adds_ema50_atr14_and_prior_volume_average(self):
        out = add_hourly_indicators(candle_frame())
        self.assertEqual(
            {"ema50", "atr14", "vol_ma20_prev"}.issubset(out.columns),
            True,
        )
        self.assertTrue(np.isfinite(out.iloc[-1]["ema50"]))
        self.assertTrue(np.isfinite(out.iloc[-1]["atr14"]))
        self.assertEqual(out.iloc[-1]["vol_ma20_prev"], 100.0)

    def test_volume_average_excludes_current_breakout_candle(self):
        frame = candle_frame()
        frame.loc[frame.index[-1], "v"] = 1_000.0
        out = add_hourly_indicators(frame)
        self.assertEqual(out.iloc[-1]["vol_ma20_prev"], 100.0)


PATTERN_ROWS = {
    "bullish_engulfing": [
        (101.0, 102.0, 98.0, 99.0),
        (98.5, 102.0, 98.0, 101.5),
    ],
    "bearish_engulfing": [
        (99.0, 102.0, 98.0, 101.0),
        (101.5, 102.0, 98.0, 98.5),
    ],
    "hammer": [
        (100.5, 101.2, 98.5, 101.0),
    ],
    "shooting_star": [
        (100.5, 102.5, 99.8, 100.0),
    ],
    "morning_star": [
        (102.0, 102.5, 97.5, 98.0),
        (99.2, 100.0, 98.8, 99.6),
        (99.5, 101.0, 99.0, 100.5),
    ],
    "evening_star": [
        (98.0, 102.5, 97.5, 102.0),
        (100.4, 101.2, 100.0, 100.8),
        (100.5, 101.0, 99.0, 99.5),
    ],
}


def make_daily_pattern(kind):
    frame = pd.DataFrame({
        "ot": np.arange(40, dtype=np.int64) * 86_400_000,
        "o": np.full(40, 100.0),
        "h": np.full(40, 101.0),
        "l": np.full(40, 99.0),
        "c": np.full(40, 100.0),
        "v": np.full(40, 100.0),
    })
    values = PATTERN_ROWS[kind]
    start = len(frame) - len(values)
    for index, (open_, high, low, close) in enumerate(values, start=start):
        frame.loc[index, ["o", "h", "l", "c"]] = [open_, high, low, close]
    return frame


class DailyConfirmationTests(unittest.TestCase):
    def test_directional_strong_daily_candle(self):
        frame = candle_frame(40)
        frame.loc[frame.index[-1], ["o", "h", "l", "c", "v"]] = [
            100.0, 112.0, 99.0, 111.0, 300.0
        ]
        result = daily_confirmation(frame, "LONG")
        self.assertEqual(result, {"passed": True, "kind": "strong_momentum", "rank": 3})
        self.assertFalse(daily_confirmation(frame, "SHORT")["passed"])

    def test_confirmed_bottom_and_top_fractals(self):
        bottom = candle_frame(40)
        bottom.loc[37, "l"], bottom.loc[38, "l"], bottom.loc[39, "l"] = 90.0, 80.0, 91.0
        self.assertEqual(
            daily_confirmation(bottom, "LONG")["kind"],
            "bottom_fractal",
        )
        top = candle_frame(40)
        top.loc[37, "h"], top.loc[38, "h"], top.loc[39, "h"] = 110.0, 120.0, 109.0
        self.assertEqual(
            daily_confirmation(top, "SHORT")["kind"],
            "top_fractal",
        )

    def test_reversal_pattern_matrix(self):
        expected = {
            "LONG": {"bullish_engulfing", "hammer", "morning_star"},
            "SHORT": {"bearish_engulfing", "shooting_star", "evening_star"},
        }
        for direction, kinds in expected.items():
            observed = {
                daily_confirmation(make_daily_pattern(kind), direction)["kind"]
                for kind in kinds
            }
            self.assertEqual(observed, kinds)

    def test_rejects_insufficient_history(self):
        result = daily_confirmation(candle_frame(2), "LONG")
        self.assertEqual(result, {"passed": False, "kind": "none", "rank": 0})


if __name__ == "__main__":
    unittest.main()
