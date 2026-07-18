"""Pure market-regime filters shared by live trading and replay."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _column(frame: pd.DataFrame, short_name: str, long_name: str) -> pd.Series:
    name = short_name if short_name in frame.columns else long_name
    if name not in frame.columns:
        raise KeyError(f"missing_column:{short_name}")
    return pd.to_numeric(frame[name], errors="coerce").reset_index(drop=True)


def _wilder_rma(values: pd.Series, length: int) -> pd.Series:
    source = pd.to_numeric(values, errors="coerce").reset_index(drop=True)
    result = pd.Series(np.nan, index=source.index, dtype=float)
    if length <= 0 or len(source) < length:
        return result
    seed = float(source.iloc[:length].mean())
    if not np.isfinite(seed):
        return result
    result.iloc[length - 1] = seed
    for index in range(length, len(source)):
        current = float(source.iloc[index])
        previous = float(result.iloc[index - 1])
        if not np.isfinite(current) or not np.isfinite(previous):
            continue
        result.iloc[index] = (previous * (length - 1) + current) / length
    return result


def _ema_rma(values: pd.Series, length: int) -> pd.Series:
    return values.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def _adx_value(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float | None:
    previous = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous).abs(), (low - previous).abs()],
        axis=1,
    ).max(axis=1)
    atr = _ema_rma(true_range, period)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_di = 100.0 * _ema_rma(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * _ema_rma(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    value = float(_ema_rma(dx, period).iloc[-1])
    return value if np.isfinite(value) else None


def _efficiency_ratio(close: pd.Series, period: int) -> float | None:
    if len(close) < period + 1:
        return None
    window = close.iloc[-period - 1:]
    path = float(window.diff().abs().sum())
    if not np.isfinite(path) or path <= 0:
        return None
    value = abs(float(window.iloc[-1] - window.iloc[0])) / path
    return value if np.isfinite(value) else None


def _empty_state(reason: str, anchor_idx: int | None = None) -> dict[str, Any]:
    return {
        "choppy_filter_available": False,
        "choppy_filter_is_choppy": False,
        "choppy_filter_reason": reason,
        "choppy_filter_reasons": [],
        "choppy_filter_anchor_idx": anchor_idx,
        "choppy_filter_anchor_time": None,
        "choppy_atr": None,
        "choppy_atr_baseline": None,
        "choppy_atr_ratio": None,
        "choppy_box_high": None,
        "choppy_box_low": None,
        "choppy_box_amplitude": None,
        "choppy_box_threshold": None,
        "choppy_box_position": None,
    }


def evaluate_choppy_market_adaptive(
    df: pd.DataFrame,
    anchor_idx: int | None = None,
    short_atr_len: int = 14,
    baseline_len: int = 100,
    box_len: int = 48,
) -> dict[str, Any]:
    """Evaluate the adaptive choppy regime at one closed signal-key candle."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return _empty_state("insufficient_data")
    if min(short_atr_len, baseline_len, box_len) <= 0:
        return _empty_state("invalid_parameters")

    resolved_idx = len(df) - 1 if anchor_idx is None else int(anchor_idx)
    if resolved_idx < 0:
        resolved_idx += len(df)
    if resolved_idx < 0 or resolved_idx >= len(df):
        return _empty_state("invalid_anchor", resolved_idx)

    required = max(short_atr_len + baseline_len - 1, box_len + 1)
    if resolved_idx + 1 < required:
        return _empty_state("insufficient_data", resolved_idx)

    try:
        context = df.iloc[:resolved_idx + 1]
        high = _column(context, "h", "high")
        low = _column(context, "l", "low")
        close = _column(context, "c", "close")
        if not np.isfinite(high).all() or not np.isfinite(low).all() or not np.isfinite(close).all():
            return _empty_state("invalid_data", resolved_idx)
        if (low <= 0).any() or (close <= 0).any() or (high < low).any():
            return _empty_state("invalid_data", resolved_idx)

        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = _wilder_rma(true_range, short_atr_len)
        atr_baseline = atr.rolling(baseline_len, min_periods=baseline_len).mean()
        current_atr = float(atr.iloc[-1])
        baseline_atr = float(atr_baseline.iloc[-1])
        current_close = float(close.iloc[-1])
        if not all(np.isfinite(v) and v > 0 for v in (current_atr, baseline_atr, current_close)):
            return _empty_state("invalid_data", resolved_idx)

        prior_box = context.iloc[-box_len - 1:-1]
        box_high = float(_column(prior_box, "h", "high").max())
        box_low = float(_column(prior_box, "l", "low").min())
        if not np.isfinite(box_high) or not np.isfinite(box_low) or box_low <= 0 or box_high <= box_low:
            return _empty_state("invalid_box", resolved_idx)

        atr_ratio = current_atr / baseline_atr
        box_amplitude = (box_high - box_low) / box_low
        box_threshold = current_atr * 2.5 / current_close
        box_position = (current_close - box_low) / (box_high - box_low)
        reasons = []
        if atr_ratio < 0.70:
            reasons.append("atr_contraction")
        if box_amplitude < box_threshold:
            reasons.append("box_squeeze")
        if 0.40 < box_position < 0.60:
            reasons.append("middle_chop")

        anchor_time = None
        if "ot" in context.columns:
            raw_time = pd.to_numeric(context["ot"], errors="coerce").iloc[-1]
            if pd.notna(raw_time):
                anchor_time = int(raw_time)
        return {
            "choppy_filter_available": True,
            "choppy_filter_is_choppy": bool(reasons),
            "choppy_filter_reason": reasons[0] if reasons else "pass",
            "choppy_filter_reasons": reasons,
            "choppy_filter_anchor_idx": resolved_idx,
            "choppy_filter_anchor_time": anchor_time,
            "choppy_atr": current_atr,
            "choppy_atr_baseline": baseline_atr,
            "choppy_atr_ratio": atr_ratio,
            "choppy_box_high": box_high,
            "choppy_box_low": box_low,
            "choppy_box_amplitude": box_amplitude,
            "choppy_box_threshold": box_threshold,
            "choppy_box_position": box_position,
        }
    except (KeyError, TypeError, ValueError, IndexError):
        return _empty_state("invalid_data", resolved_idx)


def evaluate_predicta_choppy_market(
    df: pd.DataFrame,
    anchor_idx: int | None = None,
    adx_period: int = 14,
    efficiency_period: int = 20,
    adx_threshold: float = 18.0,
    efficiency_threshold: float = 0.20,
) -> dict[str, Any]:
    """Add weak-direction detection to the existing Predicta choppy state."""
    state = dict(evaluate_choppy_market_adaptive(df, anchor_idx=anchor_idx))
    state.update({
        "choppy_adx_period": adx_period,
        "choppy_adx": None,
        "choppy_efficiency_period": efficiency_period,
        "choppy_efficiency_ratio": None,
    })
    if not state.get("choppy_filter_available"):
        return state

    resolved_idx = len(df) - 1 if anchor_idx is None else int(anchor_idx)
    if resolved_idx < 0:
        resolved_idx += len(df)
    try:
        context = df.iloc[:resolved_idx + 1]
        high = _column(context, "h", "high")
        low = _column(context, "l", "low")
        close = _column(context, "c", "close")
        adx = _adx_value(high, low, close, adx_period)
        efficiency = _efficiency_ratio(close, efficiency_period)
    except (KeyError, TypeError, ValueError, IndexError):
        return state

    state["choppy_adx"] = adx
    state["choppy_efficiency_ratio"] = efficiency
    if (
        adx is not None
        and efficiency is not None
        and adx < adx_threshold
        and efficiency < efficiency_threshold
    ):
        reasons = list(state.get("choppy_filter_reasons") or [])
        if "weak_directional_efficiency" not in reasons:
            reasons.append("weak_directional_efficiency")
        state["choppy_filter_is_choppy"] = True
        state["choppy_filter_reasons"] = reasons
        if state.get("choppy_filter_reason") == "pass":
            state["choppy_filter_reason"] = "weak_directional_efficiency"
    return state


def is_choppy_market_adaptive(
    df: pd.DataFrame,
    anchor_idx: int | None = None,
    short_atr_len: int = 14,
    baseline_len: int = 100,
    box_len: int = 48,
) -> bool:
    state = evaluate_choppy_market_adaptive(
        df,
        anchor_idx=anchor_idx,
        short_atr_len=short_atr_len,
        baseline_len=baseline_len,
        box_len=box_len,
    )
    return bool(state["choppy_filter_is_choppy"])
