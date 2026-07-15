"""Pure Predicta V4 BUY/SELL labels and EWO confirmation state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PredictaParams:
    ema_fast: int = 8
    ema_slow: int = 21
    supertrend_atr_period: int = 10
    supertrend_factor: float = 3.0
    ewo_fast: int = 5
    ewo_slow: int = 35
    confirm_bars: int = 6
    confirm_atr_buffer: float = 0.08
    stop_atr_mult: float = 0.5


@dataclass(frozen=True)
class PredictaSetupDecision:
    status: Literal["waiting", "confirmed", "invalidated", "timeout"]
    reason: str
    age_bars: int
    confirm_time: int | None = None
    confirm_price: float = 0.0
    stop_price: float = 0.0
    ewo: float = 0.0


def _rma(series: pd.Series, period: int) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    output = np.full(len(values), np.nan, dtype=float)
    period = max(1, int(period))
    if period == 1:
        return pd.Series(values, index=series.index, dtype=float)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        if np.isfinite(window).all():
            output[index] = float(window.mean())
            start = index + 1
            break
    else:
        return pd.Series(output, index=series.index, dtype=float)
    for index in range(start, len(values)):
        if np.isfinite(values[index]):
            output[index] = (output[index - 1] * (period - 1) + values[index]) / period
    return pd.Series(output, index=series.index, dtype=float)


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous = frame["c"].shift(1)
    true_range = pd.concat([
        frame["h"] - frame["l"],
        (frame["h"] - previous).abs(),
        (frame["l"] - previous).abs(),
    ], axis=1).max(axis=1)
    return _rma(true_range, period)


def compute_predicta(df: pd.DataFrame, params: PredictaParams | None = None) -> pd.DataFrame:
    params = params or PredictaParams()
    required = {"o", "h", "l", "c", "v"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"predicta_missing_columns:{','.join(sorted(missing))}")
    frame = df.copy().reset_index(drop=True)
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["ema8"] = frame["c"].ewm(span=max(1, params.ema_fast), adjust=False).mean()
    frame["ema21"] = frame["c"].ewm(span=max(1, params.ema_slow), adjust=False).mean()
    frame["trend_atr"] = _atr(frame, params.supertrend_atr_period)
    middle = (frame["h"] + frame["l"]) / 2.0
    raw_upper = middle + float(params.supertrend_factor) * frame["trend_atr"]
    raw_lower = middle - float(params.supertrend_factor) * frame["trend_atr"]
    upper = np.full(len(frame), np.nan, dtype=float)
    lower = np.full(len(frame), np.nan, dtype=float)
    direction = np.ones(len(frame), dtype=int)

    for index in range(len(frame)):
        if not np.isfinite(raw_upper.iloc[index]) or not np.isfinite(raw_lower.iloc[index]):
            if index > 0:
                direction[index] = direction[index - 1]
            continue
        previous_upper = upper[index - 1] if index > 0 and np.isfinite(upper[index - 1]) else float(raw_upper.iloc[index])
        previous_lower = lower[index - 1] if index > 0 and np.isfinite(lower[index - 1]) else float(raw_lower.iloc[index])
        previous_close = float(frame["c"].iloc[index - 1]) if index > 0 else float(frame["c"].iloc[index])
        lower[index] = max(float(raw_lower.iloc[index]), previous_lower) if previous_close > previous_lower else float(raw_lower.iloc[index])
        upper[index] = min(float(raw_upper.iloc[index]), previous_upper) if previous_close < previous_upper else float(raw_upper.iloc[index])
        previous_direction = direction[index - 1] if index > 0 else 1
        close = float(frame["c"].iloc[index])
        if previous_direction == -1:
            direction[index] = 1 if close < lower[index] else -1
        else:
            direction[index] = -1 if close > upper[index] else 1

    frame["upper_band"] = upper
    frame["lower_band"] = lower
    frame["trend_direction"] = direction
    frame["is_uptrend"] = frame["trend_direction"].eq(-1)
    frame["is_downtrend"] = frame["trend_direction"].eq(1)

    candle_range = frame["h"] - frame["l"]
    buy_volume = np.where(
        candle_range > 0,
        frame["v"] * (frame["c"] - frame["l"]) / candle_range,
        frame["v"] * 0.5,
    )
    sell_volume = np.where(
        candle_range > 0,
        frame["v"] * (frame["h"] - frame["c"]) / candle_range,
        frame["v"] * 0.5,
    )
    frame["delta"] = buy_volume - sell_volume

    cross_up = frame["ema8"].shift(1).le(frame["ema21"].shift(1)) & frame["ema8"].gt(frame["ema21"])
    cross_down = frame["ema8"].shift(1).ge(frame["ema21"].shift(1)) & frame["ema8"].lt(frame["ema21"])
    frame["bull_signal"] = (cross_up & frame["is_uptrend"] & frame["delta"].gt(0)).fillna(False)
    frame["bear_signal"] = (cross_down & frame["is_downtrend"] & frame["delta"].lt(0)).fillna(False)
    frame["ewo"] = (
        frame["c"].rolling(max(1, params.ewo_fast)).mean()
        - frame["c"].rolling(max(1, params.ewo_slow)).mean()
    )
    return frame


def make_predicta_setup(
    symbol: str,
    direction: str,
    interval: str,
    frame: pd.DataFrame,
    signal_index: int,
    signal_ewo: float,
    choppy_state: dict,
    params: PredictaParams | None = None,
) -> dict:
    params = params or PredictaParams()
    row = frame.iloc[int(signal_index)]
    direction = str(direction).upper()
    key_time = int(float(row["ot"]))
    key_high = float(row["h"])
    key_low = float(row["l"])
    signal_ewo = float(signal_ewo)
    aligned = (direction == "LONG" and signal_ewo > 0) or (direction == "SHORT" and signal_ewo < 0)
    return {
        "symbol": str(symbol),
        "direction": direction,
        "source_interval": str(interval),
        "source_strategy": "predicta_ewo",
        "signal_key": (
            f"PREDICTA|{symbol}|{direction}|{interval}|{key_time}|"
            f"{key_high:.8f}|{key_low:.8f}"
        ),
        "predicta_key_time": key_time,
        "predicta_key_high": key_high,
        "predicta_key_low": key_low,
        "predicta_signal_ewo": signal_ewo,
        "predicta_entry_path": "fast" if aligned else "wait",
        "predicta_confirm_bars": int(params.confirm_bars),
        **dict(choppy_state or {}),
    }


def evaluate_predicta_setup(
    setup: dict,
    df: pd.DataFrame,
    params: PredictaParams | None,
    atr_value: float,
) -> PredictaSetupDecision:
    params = params or PredictaParams()
    frame = compute_predicta(df, params)
    if "ot" not in frame.columns:
        return PredictaSetupDecision("invalidated", "signal_bar_missing", 0)
    key_time = int(setup.get("predicta_key_time", 0) or 0)
    matches = frame.index[pd.to_numeric(frame["ot"], errors="coerce").eq(key_time)].tolist()
    if not matches:
        return PredictaSetupDecision("invalidated", "signal_bar_missing", 0)
    signal_index = int(matches[-1])
    current_index = len(frame) - 1
    age = current_index - signal_index
    if age <= 0:
        return PredictaSetupDecision("waiting", "await_next_closed_bar", age)
    if age > int(params.confirm_bars):
        return PredictaSetupDecision("timeout", "confirm_window_expired", age)

    direction = str(setup.get("direction", "")).upper()
    key_high = float(setup.get("predicta_key_high", 0.0) or 0.0)
    key_low = float(setup.get("predicta_key_low", 0.0) or 0.0)
    closes = pd.to_numeric(frame["c"].iloc[signal_index + 1:current_index + 1], errors="coerce")
    if direction == "LONG" and closes.lt(key_low).any():
        return PredictaSetupDecision("invalidated", "opposite_key_break", age)
    if direction == "SHORT" and closes.gt(key_high).any():
        return PredictaSetupDecision("invalidated", "opposite_key_break", age)

    current = frame.iloc[current_index]
    close = float(current["c"])
    ewo = float(current["ewo"]) if pd.notna(current["ewo"]) else float("nan")
    atr_value = max(0.0, float(atr_value or 0.0))
    confirm_line = (
        key_high + atr_value * float(params.confirm_atr_buffer)
        if direction == "LONG"
        else key_low - atr_value * float(params.confirm_atr_buffer)
    )
    price_ok = close > confirm_line if direction == "LONG" else close < confirm_line
    ewo_ok = np.isfinite(ewo) and (ewo > 0 if direction == "LONG" else ewo < 0)
    if not price_ok:
        return PredictaSetupDecision("waiting", "await_price_break", age, ewo=ewo)
    if not ewo_ok:
        return PredictaSetupDecision("waiting", "await_ewo", age, ewo=ewo)

    stop = (
        key_low - atr_value * float(params.stop_atr_mult)
        if direction == "LONG"
        else key_high + atr_value * float(params.stop_atr_mult)
    )
    return PredictaSetupDecision(
        status="confirmed",
        reason="predicta_key_break_ewo",
        age_bars=age,
        confirm_time=int(float(current["ot"])),
        confirm_price=close,
        stop_price=stop,
        ewo=ewo,
    )
