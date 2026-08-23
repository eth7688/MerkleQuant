import unittest
from unittest.mock import patch
from pathlib import Path
import sys
import hashlib
import json
import math

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import momentum_compression as compression_module
from benchmark_momentum_compression import frame_for as benchmark_frame_for

from momentum_compression import (
    CompressionParams,
    _classify_without_episode,
    _common_structure,
    _quality_score,
    _non_length_rules,
    _maximal_structural_suffix,
    _pivots,
    _range_contraction_ratio,
    _touch_events,
    add_compression_indicators,
    evaluate_both_sides,
    evaluate_side,
)


BASE_OT = 1_700_000_000_000
LEGACY_REQUIRED_COLUMNS = ("ot", "o", "h", "l", "c", "v")
FLOAT_TOLERANCE = 1e-12  # Detects rule-threshold drift while allowing arithmetic order noise.
LUMIA_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "lumiausdt_binance_futures_15m_20260823_1300.json"
)


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


def range_frame(ranges):
    return pd.DataFrame({
        "h": [100.0 + value for value in ranges],
        "l": [100.0] * len(ranges),
    })


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


def legacy_add_compression_indicators(frame):
    out = frame.copy().reset_index(drop=True)
    previous_close = out["c"].shift(1)
    true_range = pd.concat((
        out["h"] - out["l"],
        (out["h"] - previous_close).abs(),
        (out["l"] - previous_close).abs(),
    ), axis=1).max(axis=1)
    out["ema8"] = out["c"].ewm(span=8, adjust=False).mean()
    out["ema21"] = out["c"].ewm(span=21, adjust=False).mean()
    out["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    return out


def legacy_fit_shifted_envelope(frame, pivot_highs, pivot_lows):
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return {}
    indexes = np.arange(len(frame), dtype=float)
    upper_slope, upper_intercept = np.polyfit(pivot_highs, frame["h"].iloc[pivot_highs], 1)
    lower_slope, lower_intercept = np.polyfit(pivot_lows, frame["l"].iloc[pivot_lows], 1)
    fitted_upper = pd.Series(upper_slope * indexes + upper_intercept)
    fitted_lower = pd.Series(lower_slope * indexes + lower_intercept)
    return {
        "upper": fitted_upper + (frame["h"].reset_index(drop=True) - fitted_upper).max(),
        "lower": fitted_lower + (frame["l"].reset_index(drop=True) - fitted_lower).min(),
        "upper_slope": float(upper_slope),
        "lower_slope": float(lower_slope),
    }


def legacy_common_structure(frame, params):
    pivot_highs, pivot_lows = legacy_pivots(frame, params.pivot_span)
    envelope = legacy_fit_shifted_envelope(frame, pivot_highs, pivot_lows)
    if not envelope:
        return {"pivot_highs": pivot_highs, "pivot_lows": pivot_lows, "envelope": {}}
    third = len(frame) // 3
    ranges = frame["h"].reset_index(drop=True) - frame["l"].reset_index(drop=True)
    first_mean = float(ranges.iloc[:third].mean()) if third else float("nan")
    last_mean = float(ranges.iloc[-third:].mean()) if third else float("nan")
    contraction_ratio = (
        last_mean / first_mean
        if math.isfinite(first_mean) and first_mean > 0 and math.isfinite(last_mean)
        else float("inf")
    )
    return {
        "pivot_highs": pivot_highs,
        "pivot_lows": pivot_lows,
        "envelope": envelope,
        "contraction_ratio": contraction_ratio,
    }


def legacy_touch_events(values, boundary, atr, tolerance):
    values, boundary, atr = (series.reset_index(drop=True) for series in (values, boundary, atr))
    touching = (values - boundary).abs() <= atr * tolerance
    events, start = [], None
    for index, is_touching in enumerate(touching):
        if is_touching and start is None:
            start = index
        if start is not None and (not is_touching or index == len(touching) - 1):
            end = index if is_touching else index - 1
            distances = (values.iloc[start:end + 1] - boundary.iloc[start:end + 1]).abs()
            events.append(start + int(np.argmin(distances.to_numpy())))
            start = None
    return events


def legacy_swing_structure(frame, pivot_highs, pivot_lows, side):
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return {"valid": False, "higher_high": False, "higher_low": False,
                "lower_low": False, "lower_high": False}
    high_change = frame["h"].iloc[pivot_highs[-1]] - frame["h"].iloc[pivot_highs[-2]]
    low_change = frame["l"].iloc[pivot_lows[-1]] - frame["l"].iloc[pivot_lows[-2]]
    result = {
        "higher_high": bool(high_change > 0), "higher_low": bool(low_change > 0),
        "lower_high": bool(high_change < 0), "lower_low": bool(low_change < 0),
    }
    result["valid"] = (
        result["higher_high"] and result["higher_low"]
        if side == "LONG" else result["lower_high"] and result["lower_low"]
    )
    return result


def legacy_non_length_rules(frame, side, params):
    common = legacy_common_structure(frame, params)
    pivot_highs, pivot_lows, envelope = (
        common["pivot_highs"], common["pivot_lows"], common["envelope"]
    )
    reasons = []
    if not envelope:
        return {
            "rejection_reasons": ["INSUFFICIENT_PIVOTS"],
            "pivot_highs": pivot_highs, "pivot_lows": pivot_lows, "envelope": envelope,
        }
    upper, lower = envelope["upper"], envelope["lower"]
    atr = frame["atr14"].replace(0, np.nan)
    ema8, ema21 = frame["ema8"], frame["ema21"]
    if side == "LONG":
        if not bool((ema8 > ema21).all()):
            reasons.append("EMA_DIRECTION")
        if not bool((frame["c"] > pd.concat((ema8, ema21), axis=1).max(axis=1)).all()):
            reasons.append("CLOSE_IN_EMA_BAND")
        directional_events = legacy_touch_events(frame["l"], lower, atr, params.touch_tolerance_atr)
    else:
        if not bool((ema8 < ema21).all()):
            reasons.append("EMA_DIRECTION")
        if not bool((frame["c"] < pd.concat((ema8, ema21), axis=1).min(axis=1)).all()):
            reasons.append("CLOSE_IN_EMA_BAND")
        directional_events = legacy_touch_events(frame["h"], upper, atr, params.touch_tolerance_atr)
    last_atr = float(atr.iloc[-1])
    ema_distance_atr = (
        abs(float(frame["c"].iloc[-1] - ema8.iloc[-1])) / last_atr
        if math.isfinite(last_atr) and last_atr > 0
        else float("inf")
    )
    if ema_distance_atr > params.max_ema_distance_atr:
        reasons.append("EMA_DISTANCE_TOO_WIDE")
    swing = legacy_swing_structure(frame, pivot_highs, pivot_lows, side)
    if not swing["valid"]:
        reasons.append("INVALID_SWING_STRUCTURE")
    if common["contraction_ratio"] > params.contraction_ratio_max:
        reasons.append("INSUFFICIENT_CONTRACTION")
    if len(directional_events) < params.min_directional_boundary_touches:
        reasons.append("INSUFFICIENT_DIRECTIONAL_TOUCHES")
    return {
        "rejection_reasons": reasons, "pivot_highs": pivot_highs, "pivot_lows": pivot_lows,
        "envelope": envelope, "directional_events": directional_events,
        "swing": swing, "contraction_ratio": common["contraction_ratio"],
        "ema_distance_atr": ema_distance_atr,
    }


def legacy_maximal_structural_suffix(indicators, side, params):
    if indicators.empty:
        return indicators, {"rejection_reasons": ["EMPTY_DATA"]}
    selected = indicators
    minimum = 2 * params.pivot_span + 1
    selected_rules = legacy_non_length_rules(indicators, side, params)
    for start in range(len(indicators)):
        candidate = indicators.iloc[start:].reset_index(drop=True)
        if len(candidate) < minimum:
            continue
        rules = legacy_non_length_rules(candidate, side, params)
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


def legacy_quality_score(metrics, params):
    contraction = max(0.0, 1.0 - float(metrics.get("contraction_ratio", 1.0)) / params.contraction_ratio_max)
    touches = min(1.0, len(metrics.get("directional_events", [])) / params.min_directional_boundary_touches)
    ema_distance = float(metrics.get("ema_distance_atr", params.max_ema_distance_atr))
    alignment = max(0.0, 1.0 - ema_distance / params.max_ema_distance_atr)
    components = {
        "contraction": contraction * 50,
        "touches": touches * 30,
        "ema_proximity": alignment * 20,
    }
    return round(sum(components.values()), 2), components


def legacy_classify_without_episode(evaluation, live_price, params):
    upper, lower = evaluation["upper_boundary_price"], evaluation["lower_boundary_price"]
    buffer_price = evaluation["breakout_buffer_price"]
    if evaluation["side"] == "LONG":
        if live_price > upper + buffer_price or live_price < lower:
            return "OUTSIDE_AT_DISCOVERY"
        if live_price >= upper:
            return "BREAKOUT_UNCONFIRMED_LONG"
        if upper - live_price <= params.pre_breakout_distance_atr * evaluation["atr14"]:
            return "PRE_BREAKOUT"
        return "COMPRESSION_ACTIVE_LONG"
    if live_price < lower - buffer_price or live_price > upper:
        return "OUTSIDE_AT_DISCOVERY"
    if live_price <= lower:
        return "BREAKOUT_UNCONFIRMED_SHORT"
    if live_price - lower <= params.pre_breakout_distance_atr * evaluation["atr14"]:
        return "PRE_BREAKOUT"
    return "COMPRESSION_ACTIVE_SHORT"


def legacy_compression_identity(evaluation):
    fields = (
        evaluation.get("symbol", ""), evaluation.get("side", ""),
        evaluation.get("compression_start_time", ""), evaluation.get("compression_end_time", ""),
        evaluation.get("parameter_version", ""),
    )
    return hashlib.sha256("|".join(map(str, fields)).encode("utf-8")).hexdigest()[:24]


def legacy_rejected(symbol, side, evaluated_at_ms, htf_alignment, reasons, params, bars=0, frame=None):
    frame = frame if frame is not None else pd.DataFrame()
    result = {
        "symbol": symbol, "side": side, "state": "REJECTED", "evaluated_at": evaluated_at_ms,
        "htf_alignment": htf_alignment, "parameter_version": params.version,
        "rejection_reasons": list(dict.fromkeys(reasons)), "compression_bars": bars,
        "compression_start_time": int(frame["ot"].iloc[0]) if bars and "ot" in frame else 0,
        "compression_end_time": int(frame["ot"].iloc[-1]) if bars and "ot" in frame else 0,
        "upper_boundary_price": None, "lower_boundary_price": None, "atr14": None,
        "breakout_buffer_price": None, "directional_touch_times": [], "score_components": {},
    }
    result["compression_id"] = legacy_compression_identity(result)
    return result


def independent_legacy_evaluate_side(
    symbol, side, closed_15m, live_price, *, evaluated_at_ms, htf_alignment,
    params=CompressionParams(),
):
    reasons = []
    if side not in ("LONG", "SHORT"):
        reasons.append("INVALID_SIDE")
    if not isinstance(live_price, (int, float, np.number)) or not math.isfinite(float(live_price)):
        reasons.append("INVALID_LIVE_PRICE")
    if closed_15m is None or closed_15m.empty:
        reasons.append("EMPTY_DATA")
    elif any(column not in closed_15m.columns for column in LEGACY_REQUIRED_COLUMNS):
        reasons.append("MISSING_REQUIRED_COLUMNS")
    if reasons:
        return legacy_rejected(symbol, side, evaluated_at_ms, htf_alignment, reasons, params)
    frame = closed_15m.loc[:, LEGACY_REQUIRED_COLUMNS].copy().reset_index(drop=True)
    frame["ot"] = pd.to_numeric(frame["ot"], errors="coerce")
    frame = frame[frame["ot"] + 900_000 <= evaluated_at_ms].reset_index(drop=True)
    if frame.empty:
        return legacy_rejected(symbol, side, evaluated_at_ms, htf_alignment, ["NO_CLOSED_CANDLES"], params)
    for column in LEGACY_REQUIRED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not np.isfinite(frame.to_numpy(dtype=float)).all():
        return legacy_rejected(symbol, side, evaluated_at_ms, htf_alignment, ["NONFINITE_OHLCV"], params, len(frame), frame)
    indicators = legacy_add_compression_indicators(frame)
    window, metrics = legacy_maximal_structural_suffix(indicators, side, params)
    bars = len(window)
    reasons = list(metrics.get("rejection_reasons", []))
    if bars < params.min_bars:
        reasons.append("WINDOW_TOO_SHORT")
    if bars > params.max_bars:
        reasons.append("WINDOW_TOO_LONG")
    envelope = metrics.get("envelope", {})
    if reasons:
        return legacy_rejected(symbol, side, evaluated_at_ms, htf_alignment, reasons, params, bars, window)
    upper, lower = envelope["upper"], envelope["lower"]
    atr14 = float(window["atr14"].iloc[-1])
    score, score_components = legacy_quality_score(metrics, params)
    result = {
        "symbol": symbol, "side": side, "evaluated_at": evaluated_at_ms,
        "htf_alignment": htf_alignment, "parameter_version": params.version,
        "rejection_reasons": [], "compression_bars": bars,
        "compression_start_time": int(window["ot"].iloc[0]),
        "compression_end_time": int(window["ot"].iloc[-1]),
        "upper_boundary_price": float(upper.iloc[-1]),
        "lower_boundary_price": float(lower.iloc[-1]),
        "upper_boundary_slope": envelope["upper_slope"],
        "lower_boundary_slope": envelope["lower_slope"],
        "atr14": atr14, "ema8": float(window["ema8"].iloc[-1]),
        "ema21": float(window["ema21"].iloc[-1]),
        "ema_distance_atr": metrics["ema_distance_atr"],
        "contraction_ratio": metrics["contraction_ratio"],
        "pivot_high_count": len(metrics["pivot_highs"]),
        "pivot_low_count": len(metrics["pivot_lows"]),
        "directional_touch_count": len(metrics["directional_events"]),
        "directional_touch_times": [int(window["ot"].iloc[index]) for index in metrics["directional_events"]],
        "breakout_buffer_price": atr14 * params.breakout_buffer_atr,
        "quality_score": score, "score_components": score_components,
        "swing": metrics["swing"],
    }
    result["compression_id"] = legacy_compression_identity(result)
    result["state"] = legacy_classify_without_episode(result, float(live_price), params)
    return result


def assert_public_outputs_equal(test_case, actual, expected, path="output"):
    if isinstance(actual, dict) and isinstance(expected, dict):
        test_case.assertEqual(set(actual), set(expected), path)
        for key in actual:
            assert_public_outputs_equal(test_case, actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(actual, list) and isinstance(expected, list):
        test_case.assertEqual(len(actual), len(expected), path)
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            assert_public_outputs_equal(test_case, actual_item, expected_item, f"{path}[{index}]")
        return
    if isinstance(actual, (int, np.integer, str, bool)) or actual is None:
        test_case.assertEqual(actual, expected, path)
        return
    if isinstance(actual, (float, np.floating)):
        if math.isnan(float(actual)) or math.isnan(float(expected)):
            test_case.assertTrue(math.isnan(float(actual)) and math.isnan(float(expected)), path)
        else:
            test_case.assertTrue(
                math.isclose(float(actual), float(expected), rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE),
                f"{path}: {actual!r} != {expected!r}",
            )
        return
    test_case.assertEqual(actual, expected, path)


def oracle_valid_frame(bars, side="LONG", anomaly_index=None):
    indexes = np.arange(bars, dtype=float)
    direction = 1.0 if side == "LONG" else -1.0
    close = 100.0 + direction * 0.1 * indexes
    pulse = 0.8 * np.sin(indexes * np.pi / 3)
    half_width = 5.0 - 0.015 * indexes
    frame = pd.DataFrame({
        "ot": BASE_OT + indexes.astype(int) * 900_000,
        "o": close,
        "h": close + half_width + pulse,
        "l": close - half_width + pulse,
        "c": close,
        "v": np.full(bars, 1000.0),
    })
    if anomaly_index is not None:
        if side == "LONG":
            frame.loc[anomaly_index, "h"] += 10.0
        else:
            frame.loc[anomaly_index, "l"] -= 10.0
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


class CompressionHistoricalRegressionTests(unittest.TestCase):
    def test_lumia_binance_window_is_rejected_for_insufficient_contraction(self):
        payload = json.loads(LUMIA_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "Binance Futures /fapi/v1/klines")
        self.assertEqual(payload["symbol"], "LUMIAUSDT")
        self.assertEqual(payload["interval"], "15m")
        self.assertEqual(payload["query"]["endTime"], 1787461199999)
        canonical = json.dumps(
            payload["ohlcv"], sort_keys=True, separators=(",", ":"),
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode()).hexdigest(),
            payload["ohlcv_sha256"],
        )
        frame = pd.DataFrame(payload["ohlcv"])
        result = evaluate_side(
            payload["symbol"],
            "SHORT",
            frame,
            payload["live_price"],
            evaluated_at_ms=payload["evaluated_at_ms"],
            htf_alignment="UNKNOWN",
        )
        self.assertEqual(result["state"], "REJECTED")
        self.assertIn(
            "INSUFFICIENT_CONTRACTION", result["rejection_reasons"],
        )
        indicators = add_compression_indicators(frame)
        former_window = indicators[
            (indicators["ot"] >= payload["former_window_start_time"])
            & (indicators["ot"] <= payload["former_window_end_time"])
        ].reset_index(drop=True)
        self.assertEqual(len(former_window), 16)
        self.assertAlmostEqual(
            _range_contraction_ratio(former_window),
            1.416058394160586,
        )


class CompressionRuleTests(unittest.TestCase):
    def test_range_contraction_accepts_exact_threshold(self):
        frame = range_frame([2.0] * 5 + [9.0] + [1.3] * 5)
        self.assertAlmostEqual(_range_contraction_ratio(frame), 0.65)

    def test_range_contraction_rejects_value_above_threshold(self):
        above = 0.650001
        frame = range_frame([2.0] * 5 + [9.0] + [2.0 * above] * 5)
        self.assertGreater(_range_contraction_ratio(frame), 0.65)

    def test_range_contraction_excludes_middle_remainder(self):
        frame = range_frame([2.0] * 5 + [500.0, 700.0] + [1.0] * 5)
        self.assertAlmostEqual(_range_contraction_ratio(frame), 0.5)

    def test_range_contraction_rejects_nonpositive_first_mean(self):
        frame = range_frame([0.0] * 5 + [1.0] * 5)
        self.assertTrue(math.isinf(_range_contraction_ratio(frame)))

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

    def test_close_to_ema8_distance_accepts_exactly_one_atr(self):
        indicators = add_compression_indicators(valid_compression_frame())
        last = indicators.index[-1]
        common = _common_structure(indicators, CompressionParams())
        common["contraction_ratio"] = 0.65
        indicators.loc[last, ["ema8", "ema21", "atr14", "c"]] = [
            100.0, 99.0, 1.0, 101.0,
        ]
        rules = _non_length_rules(
            indicators, "LONG", CompressionParams(), common=common,
        )
        self.assertNotIn("EMA_DISTANCE_TOO_WIDE", rules["rejection_reasons"])
        self.assertAlmostEqual(rules["ema_distance_atr"], 1.0)

    def test_close_to_ema8_distance_rejects_above_one_atr(self):
        indicators = add_compression_indicators(valid_compression_frame())
        last = indicators.index[-1]
        common = _common_structure(indicators, CompressionParams())
        common["contraction_ratio"] = 0.65
        indicators.loc[last, ["ema8", "ema21", "atr14", "c"]] = [
            100.0, 99.0, 1.0, 101.000001,
        ]
        rules = _non_length_rules(
            indicators, "LONG", CompressionParams(), common=common,
        )
        self.assertIn("EMA_DISTANCE_TOO_WIDE", rules["rejection_reasons"])
        self.assertGreater(rules["ema_distance_atr"], 1.0)

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

    def test_suffix_search_traverses_many_candidate_geometries_with_range_rule(self):
        frame, metadata = benchmark_frame_for(0)
        self.assertEqual(metadata["cohort"], "deep")
        indexes = np.arange(len(frame), dtype=float)
        pulse = 0.7 * np.sin(indexes * np.pi / 3)
        frame["h"] = frame["c"] + 3.0 + pulse
        frame["l"] = frame["c"] - 3.0 + pulse
        frame.loc[60, "h"] += 10.0
        evaluated_at = int(frame["ot"].iloc[-1] + 900_000)
        with patch(
            "momentum_compression._common_structure",
            wraps=compression_module._common_structure,
        ) as common:
            rows = evaluate_both_sides(
                "DEEPUSDT", frame, float(frame["c"].iloc[-1]),
                evaluated_at_ms=evaluated_at, htf_alignment_by_side={},
            )
        self.assertEqual([row["side"] for row in rows], ["LONG", "SHORT"])
        self.assertGreater(len(common.call_args_list), 20)

    def test_independent_legacy_oracle_matches_full_public_output(self):
        cases = [
            (f"{side}-{bars}", side, oracle_valid_frame(bars, side))
            for side in ("LONG", "SHORT") for bars in (14, 15, 100, 101, 220)
        ]
        cases.extend((
            ("long-active", "LONG", oracle_valid_frame(220, "LONG", anomaly_index=130)),
            ("short-active", "SHORT", oracle_valid_frame(220, "SHORT", anomaly_index=130)),
        ))
        for name, side, frame in cases:
            with self.subTest(name=name):
                self.assertTrue((frame["h"] >= frame[["o", "c"]].max(axis=1)).all())
                self.assertTrue((frame["l"] <= frame[["o", "c"]].min(axis=1)).all())
                evaluated_at = int(frame["ot"].iloc[-1] + 900_000)
                actual = evaluate_side(
                    "ORACLEUSDT", side, frame, float(frame["c"].iloc[-1]),
                    evaluated_at_ms=evaluated_at, htf_alignment="UNKNOWN",
                )
                expected = independent_legacy_evaluate_side(
                    "ORACLEUSDT", side, frame, float(frame["c"].iloc[-1]),
                    evaluated_at_ms=evaluated_at, htf_alignment="UNKNOWN",
                )
                assert_public_outputs_equal(self, actual, expected)


if __name__ == "__main__":
    unittest.main()
