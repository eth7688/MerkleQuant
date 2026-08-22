"""Pure, closed-candle rules for 15 minute momentum compression scans."""

from dataclasses import dataclass
import hashlib
import math

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = ("ot", "o", "h", "l", "c", "v")


@dataclass(frozen=True)
class CompressionParams:
    version: str = "15m-compression-v1"
    pivot_span: int = 2
    touch_tolerance_atr: float = 0.15
    contraction_ratio_max: float = 0.65
    max_ema_distance_atr: float = 1.0
    pre_breakout_distance_atr: float = 0.35
    breakout_buffer_atr: float = 0.05
    min_directional_boundary_touches: int = 3
    min_bars: int = 15
    max_bars: int = 100


def add_compression_indicators(frame: pd.DataFrame) -> pd.DataFrame:
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


def _pivots(frame: pd.DataFrame, span: int) -> tuple[list[int], list[int]]:
    width = 2 * span + 1
    if span < 0 or len(frame) < width:
        return [], []
    highs = frame["h"].to_numpy(dtype=float, copy=False)
    lows = frame["l"].to_numpy(dtype=float, copy=False)
    high_windows = np.lib.stride_tricks.sliding_window_view(highs, width)
    low_windows = np.lib.stride_tricks.sliding_window_view(lows, width)
    high_centers = highs[span:span + len(high_windows)]
    low_centers = lows[span:span + len(low_windows)]
    high_mask = (high_centers == high_windows.max(axis=1)) & (
        (high_windows == high_centers[:, None]).sum(axis=1) == 1
    )
    low_mask = (low_centers == low_windows.min(axis=1)) & (
        (low_windows == low_centers[:, None]).sum(axis=1) == 1
    )
    return (
        (np.flatnonzero(high_mask) + span).tolist(),
        (np.flatnonzero(low_mask) + span).tolist(),
    )


def _fit_shifted_envelope(frame: pd.DataFrame, pivot_highs: list[int], pivot_lows: list[int]) -> dict:
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return {}
    indexes = np.arange(len(frame), dtype=float)
    upper_slope, upper_intercept = np.polyfit(pivot_highs, frame["h"].iloc[pivot_highs], 1)
    lower_slope, lower_intercept = np.polyfit(pivot_lows, frame["l"].iloc[pivot_lows], 1)
    fitted_upper = pd.Series(upper_slope * indexes + upper_intercept)
    fitted_lower = pd.Series(lower_slope * indexes + lower_intercept)
    upper = fitted_upper + (frame["h"].reset_index(drop=True) - fitted_upper).max()
    lower = fitted_lower + (frame["l"].reset_index(drop=True) - fitted_lower).min()
    return {
        "upper": upper,
        "lower": lower,
        "upper_slope": float(upper_slope),
        "lower_slope": float(lower_slope),
    }


def _common_structure(frame: pd.DataFrame, params: CompressionParams) -> dict:
    pivot_highs, pivot_lows = _pivots(frame, params.pivot_span)
    envelope = _fit_shifted_envelope(frame, pivot_highs, pivot_lows)
    if not envelope:
        return {
            "pivot_highs": pivot_highs,
            "pivot_lows": pivot_lows,
            "envelope": {},
        }
    widths = envelope["upper"] - envelope["lower"]
    contraction_ratio = (
        float(widths.iloc[-1] / widths.iloc[0])
        if widths.iloc[0] else float("inf")
    )
    return {
        "pivot_highs": pivot_highs,
        "pivot_lows": pivot_lows,
        "envelope": envelope,
        "contraction_ratio": contraction_ratio,
    }


def _touch_events(values: pd.Series, boundary: pd.Series, atr: pd.Series, tolerance: float) -> list[int]:
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


def _swing_structure(frame: pd.DataFrame, pivot_highs: list[int], pivot_lows: list[int], side: str) -> dict:
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return {"valid": False, "higher_high": False, "higher_low": False,
                "lower_low": False, "lower_high": False}
    high_change = frame["h"].iloc[pivot_highs[-1]] - frame["h"].iloc[pivot_highs[-2]]
    low_change = frame["l"].iloc[pivot_lows[-1]] - frame["l"].iloc[pivot_lows[-2]]
    result = {
        "higher_high": bool(high_change > 0), "higher_low": bool(low_change > 0),
        "lower_high": bool(high_change < 0), "lower_low": bool(low_change < 0),
    }
    result["valid"] = result["higher_high"] and result["higher_low"] if side == "LONG" else result["lower_high"] and result["lower_low"]
    return result


def _non_length_rules(frame, side, params, *, common=None):
    common = common if common is not None else _common_structure(frame, params)
    pivot_highs = common["pivot_highs"]
    pivot_lows = common["pivot_lows"]
    envelope = common["envelope"]
    reasons = []
    if not envelope:
        reasons.append("INSUFFICIENT_PIVOTS")
        return {
            "rejection_reasons": reasons,
            "pivot_highs": pivot_highs,
            "pivot_lows": pivot_lows,
            "envelope": envelope,
        }
    upper, lower = envelope["upper"], envelope["lower"]
    atr = frame["atr14"].replace(0, np.nan)
    ema8, ema21 = frame["ema8"], frame["ema21"]
    if side == "LONG":
        if not bool((ema8 > ema21).all()):
            reasons.append("EMA_DIRECTION")
        if not bool((frame["c"] > pd.concat((ema8, ema21), axis=1).max(axis=1)).all()):
            reasons.append("CLOSE_IN_EMA_BAND")
        directional_events = _touch_events(
            frame["l"], lower, atr, params.touch_tolerance_atr
        )
    else:
        if not bool((ema8 < ema21).all()):
            reasons.append("EMA_DIRECTION")
        if not bool((frame["c"] < pd.concat((ema8, ema21), axis=1).min(axis=1)).all()):
            reasons.append("CLOSE_IN_EMA_BAND")
        directional_events = _touch_events(
            frame["h"], upper, atr, params.touch_tolerance_atr
        )
    if (
        not math.isfinite(float(atr.iloc[-1]))
        or abs(float(ema8.iloc[-1] - ema21.iloc[-1]))
        > float(atr.iloc[-1]) * params.max_ema_distance_atr
    ):
        reasons.append("EMA_DISTANCE_TOO_WIDE")
    swing = _swing_structure(frame, pivot_highs, pivot_lows, side)
    if not swing["valid"]:
        reasons.append("INVALID_SWING_STRUCTURE")
    contraction_ratio = common["contraction_ratio"]
    if contraction_ratio > params.contraction_ratio_max:
        reasons.append("INSUFFICIENT_CONTRACTION")
    if len(directional_events) < params.min_directional_boundary_touches:
        reasons.append("INSUFFICIENT_DIRECTIONAL_TOUCHES")
    return {
        "rejection_reasons": reasons, "pivot_highs": pivot_highs, "pivot_lows": pivot_lows,
        "envelope": envelope, "directional_events": directional_events,
        "swing": swing, "contraction_ratio": contraction_ratio,
    }


def _ema_candidate_start(indicators: pd.DataFrame, side: str) -> int:
    ema8 = indicators["ema8"].to_numpy(dtype=float, copy=False)
    ema21 = indicators["ema21"].to_numpy(dtype=float, copy=False)
    close = indicators["c"].to_numpy(dtype=float, copy=False)
    if side == "LONG":
        valid = (ema8 > ema21) & (close > np.maximum(ema8, ema21))
    else:
        valid = (ema8 < ema21) & (close < np.minimum(ema8, ema21))
    invalid = np.flatnonzero(~valid)
    return int(invalid[-1] + 1) if invalid.size else 0


def _maximal_structural_suffix(
    indicators, side, params, *, common_cache=None,
):
    if indicators.empty:
        return indicators, {"rejection_reasons": ["EMPTY_DATA"]}
    minimum = 2 * params.pivot_span + 1
    if len(indicators) < minimum:
        return indicators, {"rejection_reasons": ["INSUFFICIENT_PIVOTS"]}
    cache = common_cache if common_cache is not None else {}

    def rules_for(candidate):
        candidate_length = len(candidate)
        common = cache.get(candidate_length)
        if common is None:
            common = _common_structure(candidate, params)
            cache[candidate_length] = common
        return _non_length_rules(candidate, side, params, common=common)

    selected_rules = rules_for(indicators)
    if not selected_rules["rejection_reasons"]:
        return indicators, selected_rules
    if "EMA_DISTANCE_TOO_WIDE" in selected_rules["rejection_reasons"]:
        return indicators, selected_rules
    first_start = max(1, _ema_candidate_start(indicators, side))
    for start in range(first_start, len(indicators) - minimum + 1):
        candidate = indicators.iloc[start:]
        rules = rules_for(candidate)
        if not rules["rejection_reasons"]:
            return candidate, rules
    return indicators, selected_rules


def _quality_score(metrics: dict, params: CompressionParams) -> tuple[float, dict]:
    contraction = max(0.0, 1.0 - float(metrics.get("contraction_ratio", 1.0)) / params.contraction_ratio_max)
    touches = min(1.0, len(metrics.get("directional_events", [])) / params.min_directional_boundary_touches)
    ema_distance = float(metrics.get("ema_distance_atr", params.max_ema_distance_atr))
    alignment = max(0.0, 1.0 - ema_distance / params.max_ema_distance_atr)
    components = {"contraction": contraction * 50, "touches": touches * 30, "ema_proximity": alignment * 20}
    return round(sum(components.values()), 2), components


def _classify_without_episode(evaluation: dict, live_price: float, params: CompressionParams) -> str:
    upper, lower = evaluation["upper_boundary_price"], evaluation["lower_boundary_price"]
    buffer_price = evaluation["breakout_buffer_price"]
    side = evaluation["side"]
    if side == "LONG":
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


def compression_identity(evaluation: dict) -> str:
    fields = (evaluation.get("symbol", ""), evaluation.get("side", ""),
              evaluation.get("compression_start_time", ""), evaluation.get("compression_end_time", ""),
              evaluation.get("parameter_version", ""))
    return hashlib.sha256("|".join(map(str, fields)).encode("utf-8")).hexdigest()[:24]


def _rejected(symbol, side, evaluated_at_ms, htf_alignment, reasons, params, bars=0, frame=None):
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
    result["compression_id"] = compression_identity(result)
    return result


def _prepare_evaluation_frame(
    closed_15m: pd.DataFrame,
    evaluated_at_ms: int,
) -> tuple[pd.DataFrame, pd.DataFrame | None, list[str]]:
    frame = closed_15m.loc[:, REQUIRED_COLUMNS].copy().reset_index(drop=True)
    frame["ot"] = pd.to_numeric(frame["ot"], errors="coerce")
    frame = frame[frame["ot"] + 900_000 <= evaluated_at_ms].reset_index(drop=True)
    if frame.empty:
        return frame, None, ["NO_CLOSED_CANDLES"]
    for column in REQUIRED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not np.isfinite(frame.to_numpy(dtype=float)).all():
        return frame, None, ["NONFINITE_OHLCV"]
    return frame, add_compression_indicators(frame), []


def _evaluate_prepared_side(
    symbol: str,
    side: str,
    frame: pd.DataFrame,
    indicators: pd.DataFrame,
    live_price: float,
    *,
    evaluated_at_ms: int,
    htf_alignment: str,
    params: CompressionParams,
    common_cache: dict | None = None,
) -> dict:
    window, metrics = _maximal_structural_suffix(
        indicators, side, params, common_cache=common_cache
    )
    bars = len(window)
    reasons = list(metrics.get("rejection_reasons", []))
    if bars < params.min_bars:
        reasons.append("WINDOW_TOO_SHORT")
    if bars > params.max_bars:
        reasons.append("WINDOW_TOO_LONG")
    envelope = metrics.get("envelope", {})
    if reasons:
        return _rejected(
            symbol, side, evaluated_at_ms, htf_alignment,
            reasons, params, bars, window,
        )
    upper, lower = envelope["upper"], envelope["lower"]
    atr14 = float(window["atr14"].iloc[-1])
    metrics["ema_distance_atr"] = (
        abs(float(window["ema8"].iloc[-1] - window["ema21"].iloc[-1])) / atr14
        if atr14 else float("inf")
    )
    score, score_components = _quality_score(metrics, params)
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
        "directional_touch_times": [
            int(window["ot"].iloc[index]) for index in metrics["directional_events"]
        ],
        "breakout_buffer_price": atr14 * params.breakout_buffer_atr,
        "quality_score": score, "score_components": score_components,
        "swing": metrics["swing"],
    }
    result["compression_id"] = compression_identity(result)
    result["state"] = _classify_without_episode(result, float(live_price), params)
    return result


def evaluate_side(symbol: str, side: str, closed_15m: pd.DataFrame, live_price: float, *, evaluated_at_ms: int, htf_alignment: str, params: CompressionParams = CompressionParams()) -> dict:
    reasons = []
    if side not in ("LONG", "SHORT"): reasons.append("INVALID_SIDE")
    if not isinstance(live_price, (int, float, np.number)) or not math.isfinite(float(live_price)): reasons.append("INVALID_LIVE_PRICE")
    if closed_15m is None or closed_15m.empty:
        reasons.append("EMPTY_DATA")
    elif any(column not in closed_15m.columns for column in REQUIRED_COLUMNS):
        reasons.append("MISSING_REQUIRED_COLUMNS")
    if reasons:
        return _rejected(symbol, side, evaluated_at_ms, htf_alignment, reasons, params)
    frame, indicators, preparation_reasons = _prepare_evaluation_frame(
        closed_15m, evaluated_at_ms
    )
    if preparation_reasons:
        return _rejected(
            symbol, side, evaluated_at_ms, htf_alignment,
            preparation_reasons, params, len(frame), frame,
        )
    return _evaluate_prepared_side(
        symbol, side, frame, indicators, live_price,
        evaluated_at_ms=evaluated_at_ms, htf_alignment=htf_alignment,
        params=params,
    )


def evaluate_both_sides(symbol: str, closed_15m: pd.DataFrame, live_price: float, *, evaluated_at_ms: int, htf_alignment_by_side: dict[str, str], params: CompressionParams = CompressionParams()) -> list[dict]:
    reasons = []
    if not isinstance(live_price, (int, float, np.number)) or not math.isfinite(float(live_price)):
        reasons.append("INVALID_LIVE_PRICE")
    if closed_15m is None or closed_15m.empty:
        reasons.append("EMPTY_DATA")
    elif any(column not in closed_15m.columns for column in REQUIRED_COLUMNS):
        reasons.append("MISSING_REQUIRED_COLUMNS")
    if reasons:
        return [
            _rejected(
                symbol, side, evaluated_at_ms,
                htf_alignment_by_side.get(side, "UNKNOWN"), reasons, params,
            )
            for side in ("LONG", "SHORT")
        ]
    frame, indicators, preparation_reasons = _prepare_evaluation_frame(
        closed_15m, evaluated_at_ms
    )
    if preparation_reasons:
        return [
            _rejected(
                symbol, side, evaluated_at_ms,
                htf_alignment_by_side.get(side, "UNKNOWN"),
                preparation_reasons, params, len(frame), frame,
            )
            for side in ("LONG", "SHORT")
        ]
    common_cache = {}
    return [
        _evaluate_prepared_side(
            symbol, side, frame, indicators, live_price,
            evaluated_at_ms=evaluated_at_ms,
            htf_alignment=htf_alignment_by_side.get(side, "UNKNOWN"),
            params=params, common_cache=common_cache,
        )
        for side in ("LONG", "SHORT")
    ]
