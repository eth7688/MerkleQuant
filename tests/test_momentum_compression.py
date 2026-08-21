import unittest

import numpy as np
import pandas as pd

from momentum_compression import (
    CompressionParams,
    _classify_without_episode,
    _quality_score,
    _touch_events,
    add_compression_indicators,
    evaluate_both_sides,
    evaluate_side,
)


BASE_OT = 1_700_000_000_000


def compression_frame(bars=20, side="LONG"):
    indexes = np.arange(bars, dtype=float)
    center = 100.0 + (0.08 * indexes if side == "LONG" else -0.08 * indexes)
    half_width = 3.0 - (2.0 * indexes / max(bars - 1, 1))
    high = center + half_width
    low = center - half_width
    close = center + (0.15 if side == "LONG" else -0.15)
    frame = pd.DataFrame({
        "ot": BASE_OT + (indexes.astype(int) * 900_000),
        "o": center,
        "h": high,
        "l": low,
        "c": close,
        "v": np.full(bars, 1000.0),
    })
    return frame


class CompressionIndicatorTests(unittest.TestCase):
    def test_default_parameters_match_approved_spec(self):
        params = CompressionParams()
        self.assertEqual(params.pivot_span, 2)
        self.assertEqual(params.touch_tolerance_atr, 0.15)
        self.assertEqual(params.contraction_ratio_max, 0.65)
        self.assertEqual(params.max_ema_distance_atr, 1.0)
        self.assertEqual(params.pre_breakout_distance_atr, 0.35)
        self.assertEqual(params.breakout_buffer_atr, 0.05)
        self.assertEqual(params.min_bars, 15)
        self.assertEqual(params.max_bars, 100)

    def test_indicators_do_not_mutate_input(self):
        frame = compression_frame()
        result = add_compression_indicators(frame)
        self.assertNotIn("ema8", frame)
        self.assertTrue({"ema8", "ema21", "atr14"}.issubset(result.columns))

    def test_unclosed_bar_cannot_change_structure(self):
        closed = compression_frame()
        poisoned_live_bar = closed.iloc[-1].copy()
        poisoned_live_bar["ot"] += 900_000
        poisoned_live_bar["h"] *= 4
        with_live = pd.concat([closed, poisoned_live_bar.to_frame().T], ignore_index=True)
        first = evaluate_side("TESTUSDT", "LONG", closed, 101.0,
                              evaluated_at_ms=2_000_000_000_000,
                              htf_alignment="UNKNOWN")
        second = evaluate_side("TESTUSDT", "LONG", with_live.iloc[:-1], 101.0,
                               evaluated_at_ms=2_000_000_000_000,
                               htf_alignment="UNKNOWN")
        self.assertEqual(first["compression_id"], second["compression_id"])
        self.assertEqual(first["upper_boundary_price"], second["upper_boundary_price"])


class CompressionRuleTests(unittest.TestCase):
    def test_touch_run_counts_once_until_non_touch_bar_separates_it(self):
        values = pd.Series([10.0, 10.01, 9.99, 9.0, 10.01, 9.99])
        boundary = pd.Series([10.0] * len(values))
        atr = pd.Series([1.0] * len(values))
        self.assertEqual(_touch_events(values, boundary, atr, 0.02), [0, 4])

    def test_invalid_inputs_are_rejected_with_audit_shape(self):
        result = evaluate_side("TESTUSDT", "BAD", pd.DataFrame(), float("nan"),
                               evaluated_at_ms=1, htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("INVALID_SIDE", result["rejection_reasons"])
        self.assertIn("INVALID_LIVE_PRICE", result["rejection_reasons"])
        self.assertIn("EMPTY_DATA", result["rejection_reasons"])

    def test_boundary_requires_two_pivot_highs_and_two_pivot_lows(self):
        result = evaluate_side("TESTUSDT", "LONG", compression_frame(15), 100.0,
                               evaluated_at_ms=1, htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("INSUFFICIENT_PIVOTS", result["rejection_reasons"])

    def test_14_bars_rejects_without_silent_padding(self):
        result = evaluate_side("TESTUSDT", "LONG", compression_frame(14), 100.0,
                               evaluated_at_ms=1, htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("WINDOW_TOO_SHORT", result["rejection_reasons"])
        self.assertEqual(result["compression_bars"], 14)

    def test_101_bars_rejects_without_truncating_to_100(self):
        result = evaluate_side("TESTUSDT", "LONG", compression_frame(101), 100.0,
                               evaluated_at_ms=1, htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("WINDOW_TOO_LONG", result["rejection_reasons"])
        self.assertEqual(result["compression_bars"], 101)

    def test_evaluate_both_sides_always_returns_long_then_short(self):
        results = evaluate_both_sides("TESTUSDT", pd.DataFrame(), 100.0,
                                      evaluated_at_ms=1,
                                      htf_alignment_by_side={})
        self.assertEqual([row["side"] for row in results], ["LONG", "SHORT"])
        self.assertTrue(all(row["rejection_reasons"] for row in results))

    def test_boundary_equalities_do_not_classify_as_outside(self):
        evaluation = {
            "side": "LONG", "upper_boundary_price": 110.0,
            "lower_boundary_price": 100.0, "breakout_buffer_price": 1.0,
            "atr14": 2.0,
        }
        self.assertEqual(_classify_without_episode(evaluation, 110.0, CompressionParams()),
                         "BREAKOUT_UNCONFIRMED_LONG")
        self.assertEqual(_classify_without_episode(evaluation, 111.0, CompressionParams()),
                         "BREAKOUT_UNCONFIRMED_LONG")
        self.assertNotEqual(_classify_without_episode(evaluation, 100.0, CompressionParams()),
                            "OUTSIDE_AT_DISCOVERY")

    def test_short_boundary_equalities_do_not_classify_as_outside(self):
        evaluation = {
            "side": "SHORT", "upper_boundary_price": 110.0,
            "lower_boundary_price": 100.0, "breakout_buffer_price": 1.0,
            "atr14": 2.0,
        }
        self.assertEqual(_classify_without_episode(evaluation, 100.0, CompressionParams()),
                         "BREAKOUT_UNCONFIRMED_SHORT")
        self.assertEqual(_classify_without_episode(evaluation, 99.0, CompressionParams()),
                         "BREAKOUT_UNCONFIRMED_SHORT")
        self.assertNotEqual(_classify_without_episode(evaluation, 110.0, CompressionParams()),
                            "OUTSIDE_AT_DISCOVERY")

    def test_quality_score_cannot_change_a_hard_rule_rejection(self):
        score, _ = _quality_score({"contraction_ratio": 0.0,
                                   "directional_events": [1, 2, 3],
                                   "ema_distance_atr": 0.0}, CompressionParams())
        result = evaluate_side("TESTUSDT", "LONG", compression_frame(14), 100.0,
                               evaluated_at_ms=1, htf_alignment="UNKNOWN")
        self.assertGreater(score, 0.0)
        self.assertEqual(result["state"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
