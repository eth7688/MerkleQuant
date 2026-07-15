import unittest

import numpy as np
import pandas as pd

from predicta_indicator import (
    PredictaParams,
    compute_predicta,
    evaluate_predicta_setup,
    make_predicta_setup,
)


def _frame(closes):
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "ot": np.arange(len(close), dtype=np.int64) * 1_800_000,
        "o": close - 0.1,
        "h": close + 0.4,
        "l": close - 0.4,
        "c": close,
        "v": np.full(len(close), 1000.0),
    })


class PredictaIndicatorTest(unittest.TestCase):
    def test_delta_matches_original_close_location_formula(self):
        df = _frame(np.linspace(90, 110, 50))
        df.loc[20, "c"] = df.loc[20, "l"] + 0.1
        result = compute_predicta(df, PredictaParams())
        expected = df["v"] * (
            (df["c"] - df["l"]) - (df["h"] - df["c"])
        ) / (df["h"] - df["l"])
        pd.testing.assert_series_equal(result["delta"], expected, check_names=False)

    def test_zero_range_has_zero_delta(self):
        df = _frame(np.linspace(90, 110, 50))
        df.loc[20, ["h", "l", "c"]] = 100.0
        result = compute_predicta(df, PredictaParams())
        self.assertEqual(float(result.loc[20, "delta"]), 0.0)

    def test_ewo_is_sma5_minus_sma35(self):
        df = _frame(np.arange(1, 61))
        result = compute_predicta(df, PredictaParams())
        expected = df["c"].rolling(5).mean() - df["c"].rolling(35).mean()
        pd.testing.assert_series_equal(result["ewo"], expected, check_names=False)

    def test_label_formulas_include_cross_trend_and_delta(self):
        df = _frame([100.0] * 35 + [98, 96, 94, 98, 103, 108, 112, 116])
        result = compute_predicta(df, PredictaParams())
        cross_up = result["ema8"].shift(1).le(result["ema21"].shift(1)) & result["ema8"].gt(result["ema21"])
        cross_down = result["ema8"].shift(1).ge(result["ema21"].shift(1)) & result["ema8"].lt(result["ema21"])
        expected_long = cross_up & result["is_uptrend"] & result["delta"].gt(0)
        expected_short = cross_down & result["is_downtrend"] & result["delta"].lt(0)
        pd.testing.assert_series_equal(result["bull_signal"], expected_long, check_names=False)
        pd.testing.assert_series_equal(result["bear_signal"], expected_short, check_names=False)

    def test_signal_ewo_selects_fast_or_wait_path(self):
        df = _frame([100.0] * 40)
        params = PredictaParams()
        fast_long = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 35, 1.0, {}, params)
        wait_long = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 35, 0.0, {}, params)
        fast_short = make_predicta_setup("BTCUSDT", "SHORT", "30m", df, 35, -1.0, {}, params)
        wait_short = make_predicta_setup("BTCUSDT", "SHORT", "30m", df, 35, 1.0, {}, params)
        self.assertEqual(fast_long["predicta_entry_path"], "fast")
        self.assertEqual(fast_short["predicta_entry_path"], "fast")
        self.assertEqual(wait_long["predicta_entry_path"], "wait")
        self.assertEqual(wait_short["predicta_entry_path"], "wait")

    def test_waiting_setup_confirms_on_breakout_with_aligned_ewo(self):
        df = _frame([100.0] * 35 + [100.0, 102.0])
        params = PredictaParams()
        setup = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 35, -1.0, {}, params)
        decision = evaluate_predicta_setup(setup, df, params, atr_value=1.0)
        self.assertEqual(decision.status, "confirmed")
        self.assertEqual(decision.age_bars, 1)
        self.assertGreater(decision.ewo, 0)
        self.assertEqual(decision.stop_price, df.loc[35, "l"] - 0.5)

    def test_breakout_with_wrong_ewo_keeps_waiting(self):
        df = _frame(list(np.linspace(140, 100, 35)) + [100.0, 102.0])
        params = PredictaParams()
        setup = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 35, -1.0, {}, params)
        decision = evaluate_predicta_setup(setup, df, params, atr_value=1.0)
        self.assertEqual(decision.status, "waiting")
        self.assertEqual(decision.reason, "await_ewo")
        self.assertLess(decision.ewo, 0)

    def test_opposite_close_invalidates_waiting_setup(self):
        df = _frame([100.0] * 35 + [100.0, 98.0])
        params = PredictaParams()
        setup = make_predicta_setup("BTCUSDT", "LONG", "30m", df, 35, -1.0, {}, params)
        decision = evaluate_predicta_setup(setup, df, params, atr_value=1.0)
        self.assertEqual(decision.status, "invalidated")
        self.assertEqual(decision.reason, "opposite_key_break")

    def test_sixth_bar_can_confirm_but_seventh_times_out(self):
        params = PredictaParams()
        sixth = _frame([100.0] * 35 + [100.0] + [100.5] * 5 + [102.0])
        setup = make_predicta_setup("BTCUSDT", "LONG", "30m", sixth, 35, -1.0, {}, params)
        self.assertEqual(evaluate_predicta_setup(setup, sixth, params, 1.0).status, "confirmed")
        seventh = pd.concat([sixth.iloc[:-1], _frame([100.5, 102.0]).assign(
            ot=[sixth.loc[40, "ot"], sixth.loc[40, "ot"] + 1_800_000]
        )], ignore_index=True)
        self.assertEqual(evaluate_predicta_setup(setup, seventh, params, 1.0).status, "timeout")

    def test_short_confirmation_is_mirrored(self):
        df = _frame([100.0] * 35 + [100.0, 98.0])
        params = PredictaParams()
        setup = make_predicta_setup("BTCUSDT", "SHORT", "30m", df, 35, 1.0, {}, params)
        decision = evaluate_predicta_setup(setup, df, params, atr_value=1.0)
        self.assertEqual(decision.status, "confirmed")
        self.assertLess(decision.ewo, 0)
        self.assertEqual(decision.stop_price, df.loc[35, "h"] + 0.5)


if __name__ == "__main__":
    unittest.main()
