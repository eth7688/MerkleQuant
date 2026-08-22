import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import momentum_compression as compression_module

from momentum_compression import (
    CompressionParams,
    _classify_without_episode,
    _quality_score,
    _non_length_rules,
    _maximal_structural_suffix,
    _pivots,
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


def valid_compression_frame(bars=40):
    indexes = np.arange(bars, dtype=float)
    pulse = np.sin(indexes * np.pi / 3)
    return pd.DataFrame({
        "ot": BASE_OT + (indexes.astype(int) * 900_000),
        "o": 100.0 + 0.4 * indexes,
        "h": 105.0 + 0.3 * indexes + pulse,
        "l": 95.0 + 0.5 * indexes + pulse,
        "c": 100.0 + 0.4 * indexes,
        "v": np.full(bars, 1000.0),
    })


def two_touch_frame():
    indexes = np.arange(15, dtype=float)
    high = 105.0 + 0.3 * indexes
    low = 95.0 + 0.5 * indexes
    high[[3, 9]] += 2.0
    low[[5, 11]] -= 2.0
    return pd.DataFrame({
        "ot": BASE_OT + (indexes.astype(int) * 900_000),
        "o": 100.0 + 0.4 * indexes,
        "h": high,
        "l": low,
        "c": 100.0 + 0.4 * indexes,
        "v": np.full(len(indexes), 1000.0),
    })


def legacy_maximal_structural_suffix(indicators, side, params):
    if indicators.empty:
        return indicators, {"rejection_reasons": ["EMPTY_DATA"]}
    selected = indicators
    minimum = 2 * params.pivot_span + 1
    selected_rules = (
        _non_length_rules(indicators, side, params)
        if len(indicators) >= minimum
        else {"rejection_reasons": ["INSUFFICIENT_PIVOTS"]}
    )
    for start in range(len(indicators)):
        candidate = indicators.iloc[start:].reset_index(drop=True)
        if len(candidate) < minimum:
            continue
        rules = _non_length_rules(candidate, side, params)
        if not rules["rejection_reasons"]:
            return candidate, rules
    return selected, selected_rules


def legacy_pivots(frame, span):
    highs, lows = [], []
    for index in range(span, len(frame) - span):
        high_window = frame["h"].iloc[index - span:index + span + 1]
        low_window = frame["l"].iloc[index - span:index + span + 1]
        if frame["h"].iloc[index] == high_window.max() and (high_window == frame["h"].iloc[index]).sum() == 1:
            highs.append(index)
        if frame["l"].iloc[index] == low_window.min() and (low_window == frame["l"].iloc[index]).sum() == 1:
            lows.append(index)
    return highs, lows


class CompressionIndicatorTests(unittest.TestCase):
    def test_default_parameters_match_approved_spec(self):
        params = CompressionParams()
        self.assertEqual(params.pivot_span, 2)
        self.assertEqual(params.touch_tolerance_atr, 0.15)
        self.assertEqual(params.contraction_ratio_max, 0.65)
        self.assertEqual(params.max_ema_distance_atr, 1.0)
        self.assertEqual(params.pre_breakout_distance_atr, 0.35)
        self.assertEqual(params.breakout_buffer_atr, 0.05)
        self.assertEqual(params.min_directional_boundary_touches, 3)
        self.assertEqual(params.min_bars, 15)
        self.assertEqual(params.max_bars, 100)

    def test_indicators_do_not_mutate_input(self):
        frame = compression_frame()
        result = add_compression_indicators(frame)
        self.assertNotIn("ema8", frame)
        self.assertTrue({"ema8", "ema21", "atr14"}.issubset(result.columns))

    def test_unfinished_trailing_bar_is_excluded_using_evaluated_time(self):
        closed = valid_compression_frame()
        poisoned_live_bar = closed.iloc[-1].copy()
        poisoned_live_bar["ot"] += 900_000
        poisoned_live_bar["h"] *= 4
        with_live = pd.concat([closed, poisoned_live_bar.to_frame().T], ignore_index=True)
        evaluated_at_ms = int(closed["ot"].iloc[-1] + 900_000)
        first = evaluate_side("TESTUSDT", "LONG", closed, 115.0,
                              evaluated_at_ms=evaluated_at_ms,
                              htf_alignment="UNKNOWN")
        second = evaluate_side("TESTUSDT", "LONG", with_live, 115.0,
                               evaluated_at_ms=evaluated_at_ms,
                               htf_alignment="UNKNOWN")
        self.assertNotEqual(first["state"], "REJECTED")
        self.assertEqual(first["compression_id"], second["compression_id"])
        self.assertEqual(first["upper_boundary_price"], second["upper_boundary_price"])

    def test_nonfinite_unfinished_trailing_bar_is_ignored_before_validation(self):
        closed = valid_compression_frame()
        unfinished = closed.iloc[-1].copy()
        unfinished["ot"] += 900_000
        unfinished["h"] = np.nan
        with_unfinished = pd.concat([closed, unfinished.to_frame().T], ignore_index=True)
        evaluated_at_ms = int(closed["ot"].iloc[-1] + 900_000)
        first = evaluate_side("TESTUSDT", "LONG", closed, 115.0,
                              evaluated_at_ms=evaluated_at_ms, htf_alignment="UNKNOWN")
        second = evaluate_side("TESTUSDT", "LONG", with_unfinished, 115.0,
                               evaluated_at_ms=evaluated_at_ms, htf_alignment="UNKNOWN")
        self.assertEqual(first["compression_id"], second["compression_id"])
        self.assertEqual(first["upper_boundary_price"], second["upper_boundary_price"])


class CompressionRuleTests(unittest.TestCase):
    def test_vectorized_pivots_match_legacy_for_unique_and_tied_extrema(self):
        rng = np.random.default_rng(20260822)
        for bars in (5, 15, 40, 220):
            frame = valid_compression_frame(bars)
            frame["h"] += rng.normal(0.0, 0.1, bars)
            frame["l"] += rng.normal(0.0, 0.1, bars)
            if bars >= 15:
                frame.loc[7, "h"] = frame.loc[8, "h"] = max(frame.loc[7, "h"], frame.loc[8, "h"])
                frame.loc[10, "l"] = frame.loc[11, "l"] = min(frame.loc[10, "l"], frame.loc[11, "l"])
            self.assertEqual(_pivots(frame, 2), legacy_pivots(frame, 2))

    def test_both_sides_share_direction_independent_candidate_geometry(self):
        frame = valid_compression_frame(40)
        evaluated_at = int(frame["ot"].iloc[-1] + 900_000)
        with patch(
            "momentum_compression._common_structure",
            wraps=compression_module._common_structure,
        ) as common:
            evaluate_both_sides(
                "TESTUSDT", frame, 115.0,
                evaluated_at_ms=evaluated_at,
                htf_alignment_by_side={},
            )
        starts = [int(call.args[0]["ot"].iloc[0]) for call in common.call_args_list]
        self.assertEqual(len(starts), len(set(starts)))

    def test_ema_pruning_skips_only_suffixes_that_are_provably_invalid(self):
        indicators = add_compression_indicators(valid_compression_frame(220))
        indicators.loc[200, "ema8"] = indicators.loc[200, "ema21"]
        with patch("momentum_compression._non_length_rules", wraps=_non_length_rules) as rules:
            optimized_window, optimized_rules = _maximal_structural_suffix(
                indicators, "LONG", CompressionParams()
            )
        reference_window, reference_rules = legacy_maximal_structural_suffix(
            indicators, "LONG", CompressionParams()
        )
        candidate_lengths = [len(call.args[0]) for call in rules.call_args_list]
        self.assertFalse(any(20 <= length < 220 for length in candidate_lengths))
        self.assertEqual(list(optimized_window["ot"]), list(reference_window["ot"]))
        self.assertEqual(
            optimized_rules["rejection_reasons"],
            reference_rules["rejection_reasons"],
        )

    def test_pruned_suffix_matches_legacy_for_fixed_random_samples(self):
        rng = np.random.default_rng(20260822)
        for side in ("LONG", "SHORT"):
            for bars in (14, 15, 40, 100, 101, 220):
                frame = valid_compression_frame(bars)
                frame["h"] += rng.normal(0.0, 0.05, bars)
                frame["l"] += rng.normal(0.0, 0.05, bars)
                indicators = add_compression_indicators(frame)
                actual_window, actual_rules = _maximal_structural_suffix(
                    indicators, side, CompressionParams()
                )
                expected_window, expected_rules = legacy_maximal_structural_suffix(
                    indicators, side, CompressionParams()
                )
                self.assertEqual(list(actual_window["ot"]), list(expected_window["ot"]))
                self.assertEqual(
                    actual_rules["rejection_reasons"],
                    expected_rules["rejection_reasons"],
                )

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
        frame = compression_frame(15)
        result = evaluate_side("TESTUSDT", "LONG", frame, 100.0,
                               evaluated_at_ms=int(frame["ot"].iloc[-1] + 900_000), htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("INSUFFICIENT_PIVOTS", result["rejection_reasons"])

    def test_14_bars_rejects_without_silent_padding(self):
        frame = compression_frame(14)
        result = evaluate_side("TESTUSDT", "LONG", frame, 100.0,
                               evaluated_at_ms=int(frame["ot"].iloc[-1] + 900_000), htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("WINDOW_TOO_SHORT", result["rejection_reasons"])
        self.assertEqual(result["compression_bars"], 14)

    def test_101_bars_rejects_without_truncating_to_100(self):
        frame = compression_frame(101)
        result = evaluate_side("TESTUSDT", "LONG", frame, 100.0,
                               evaluated_at_ms=int(frame["ot"].iloc[-1] + 900_000), htf_alignment="UNKNOWN")
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn("WINDOW_TOO_LONG", result["rejection_reasons"])
        self.assertEqual(result["compression_bars"], 101)

    def test_evaluate_both_sides_always_returns_long_then_short(self):
        results = evaluate_both_sides("TESTUSDT", pd.DataFrame(), 100.0,
                                      evaluated_at_ms=1,
                                      htf_alignment_by_side={})
        self.assertEqual([row["side"] for row in results], ["LONG", "SHORT"])
        self.assertTrue(all(row["rejection_reasons"] for row in results))

    def test_evaluate_both_sides_prepares_indicators_once(self):
        frame = valid_compression_frame(40)
        evaluated_at = int(frame["ot"].iloc[-1] + 900_000)
        with patch(
            "momentum_compression.add_compression_indicators",
            wraps=compression_module.add_compression_indicators,
        ) as indicators:
            rows = evaluate_both_sides(
                "TESTUSDT", frame, 115.0,
                evaluated_at_ms=evaluated_at,
                htf_alignment_by_side={"LONG": "UNKNOWN", "SHORT": "UNKNOWN"},
            )
        self.assertEqual([row["side"] for row in rows], ["LONG", "SHORT"])
        self.assertEqual(indicators.call_count, 1)

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

    def test_full_candidate_window_includes_first_bar_in_ema_order_rule(self):
        indicators = add_compression_indicators(valid_compression_frame())
        indicators.loc[indicators.index[0], "ema8"] = indicators.loc[indicators.index[0], "ema21"]
        rules = _non_length_rules(indicators, "LONG", CompressionParams())
        self.assertIn("EMA_DIRECTION", rules["rejection_reasons"])

    def test_close_in_ema_band_is_a_hard_rejection(self):
        indicators = add_compression_indicators(valid_compression_frame())
        indicators.loc[indicators.index[-1], "c"] = indicators.loc[indicators.index[-1], "ema8"]
        rules = _non_length_rules(indicators, "LONG", CompressionParams())
        self.assertIn("CLOSE_IN_EMA_BAND", rules["rejection_reasons"])

    def test_ema_distance_is_a_hard_rejection(self):
        indicators = add_compression_indicators(valid_compression_frame())
        indicators.loc[indicators.index[-1], "ema8"] = indicators.loc[indicators.index[-1], "ema21"] + indicators.loc[indicators.index[-1], "atr14"] * 2
        rules = _non_length_rules(indicators, "LONG", CompressionParams())
        self.assertIn("EMA_DISTANCE_TOO_WIDE", rules["rejection_reasons"])

    def test_long_requires_three_lower_wick_events(self):
        indicators = add_compression_indicators(valid_compression_frame(15))
        rules = _non_length_rules(indicators, "LONG", CompressionParams(
            min_directional_boundary_touches=4))
        self.assertEqual(len(rules["directional_events"]), 3)
        self.assertIn("INSUFFICIENT_DIRECTIONAL_TOUCHES", rules["rejection_reasons"])

    def test_short_requires_three_upper_wick_events(self):
        indicators = add_compression_indicators(valid_compression_frame(15))
        rules = _non_length_rules(indicators, "SHORT", CompressionParams(
            min_directional_boundary_touches=4))
        self.assertEqual(len(rules["directional_events"]), 3)
        self.assertIn("INSUFFICIENT_DIRECTIONAL_TOUCHES", rules["rejection_reasons"])

    def test_long_default_threshold_rejects_exactly_two_lower_wick_runs(self):
        rules = _non_length_rules(add_compression_indicators(two_touch_frame()), "LONG", CompressionParams())
        self.assertEqual(len(rules["directional_events"]), 2)
        self.assertIn("INSUFFICIENT_DIRECTIONAL_TOUCHES", rules["rejection_reasons"])

    def test_short_default_threshold_rejects_exactly_two_upper_wick_runs(self):
        rules = _non_length_rules(add_compression_indicators(two_touch_frame()), "SHORT", CompressionParams())
        self.assertEqual(len(rules["directional_events"]), 2)
        self.assertIn("INSUFFICIENT_DIRECTIONAL_TOUCHES", rules["rejection_reasons"])

    def test_higher_high_and_higher_low_are_required_for_long(self):
        indicators = add_compression_indicators(valid_compression_frame())
        indicators.loc[34, "l"] = indicators["l"].iloc[28] - 10
        rules = _non_length_rules(indicators, "LONG", CompressionParams())
        self.assertIn("INVALID_SWING_STRUCTURE", rules["rejection_reasons"])

    def test_lower_low_and_lower_high_are_required_for_short(self):
        indicators = add_compression_indicators(valid_compression_frame())
        rules = _non_length_rules(indicators, "SHORT", CompressionParams())
        self.assertIn("INVALID_SWING_STRUCTURE", rules["rejection_reasons"])

    def test_contraction_ratio_is_a_hard_rejection(self):
        indicators = add_compression_indicators(valid_compression_frame())
        indicators["h"] = 110.0 + 0.5 * np.arange(len(indicators)) + np.sin(np.arange(len(indicators)) * np.pi / 3)
        indicators["l"] = 90.0 + 0.1 * np.arange(len(indicators)) + np.sin(np.arange(len(indicators)) * np.pi / 3)
        rules = _non_length_rules(indicators, "LONG", CompressionParams())
        self.assertIn("INSUFFICIENT_CONTRACTION", rules["rejection_reasons"])

    def test_maximal_suffix_extends_until_the_first_invalid_bar(self):
        indicators = add_compression_indicators(valid_compression_frame())
        window, rules = _maximal_structural_suffix(indicators, "LONG", CompressionParams())
        self.assertEqual(int(window["ot"].iloc[0]), int(indicators["ot"].iloc[1]))
        self.assertEqual(len(window), len(indicators) - 1)
        self.assertEqual(rules["rejection_reasons"], [])


if __name__ == "__main__":
    unittest.main()
