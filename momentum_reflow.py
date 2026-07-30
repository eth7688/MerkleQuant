from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

EMA_PERIOD = 50
ATR_PERIOD = 14
VOLUME_PERIOD = 20
BREAKOUT_BODY_ATR = 0.8
BREAKOUT_VOLUME_RATIO = 1.5
EXPANSION_ATR = 1.5
EXPANSION_FOLLOW_BARS = 3
EMA_SLOPE_BARS = 3
TOUCH_ZONE_ATR = 0.2
CLOSE_DISTANCE_ATR = 0.35
RETURN_WINDOW_BARS = 5
LEDGER_VERSION = 1


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["c"].shift(1)
    return pd.concat(
        [
            frame["h"] - frame["l"],
            (frame["h"] - previous_close).abs(),
            (frame["l"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def add_hourly_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values("ot").drop_duplicates("ot").reset_index(drop=True).copy()
    out["ema50"] = out["c"].ewm(span=EMA_PERIOD, adjust=False).mean()
    out["atr14"] = _true_range(out).rolling(ATR_PERIOD).mean()
    out["vol_ma20_prev"] = out["v"].shift(1).rolling(VOLUME_PERIOD).mean()
    return out


def add_daily_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values("ot").drop_duplicates("ot").reset_index(drop=True).copy()
    out["atr14"] = _true_range(out).rolling(ATR_PERIOD).mean()
    out["vol_ma20_prev"] = out["v"].shift(1).rolling(VOLUME_PERIOD).mean()
    return out


def _body(row) -> float:
    return abs(float(row["c"]) - float(row["o"]))


def _range(row) -> float:
    return max(0.0, float(row["h"]) - float(row["l"]))


def _bullish_engulfing(previous, current) -> bool:
    return (
        previous["c"] < previous["o"]
        and current["c"] > current["o"]
        and current["o"] <= previous["c"]
        and current["c"] >= previous["o"]
    )


def _bearish_engulfing(previous, current) -> bool:
    return (
        previous["c"] > previous["o"]
        and current["c"] < current["o"]
        and current["o"] >= previous["c"]
        and current["c"] <= previous["o"]
    )


def _hammer(row) -> bool:
    body = max(_body(row), 1e-12)
    lower = min(row["o"], row["c"]) - row["l"]
    upper = row["h"] - max(row["o"], row["c"])
    return row["c"] > row["o"] and lower >= 2.0 * body and upper <= body


def _shooting_star(row) -> bool:
    body = max(_body(row), 1e-12)
    upper = row["h"] - max(row["o"], row["c"])
    lower = min(row["o"], row["c"]) - row["l"]
    return row["c"] < row["o"] and upper >= 2.0 * body and lower <= body


def _morning_star(first, middle, third) -> bool:
    return (
        first["c"] < first["o"]
        and third["c"] > third["o"]
        and _body(first) >= 0.6 * _range(first)
        and _body(middle) <= 0.4 * _body(first)
        and third["c"] > (first["o"] + first["c"]) / 2.0
    )


def _evening_star(first, middle, third) -> bool:
    return (
        first["c"] > first["o"]
        and third["c"] < third["o"]
        and _body(first) >= 0.6 * _range(first)
        and _body(middle) <= 0.4 * _body(first)
        and third["c"] < (first["o"] + first["c"]) / 2.0
    )


def _result(kind: str, rank: int) -> dict:
    return {"passed": True, "kind": kind, "rank": rank}


def daily_confirmation(frame: pd.DataFrame, direction: str) -> dict:
    out = add_daily_indicators(frame)
    if direction not in {"LONG", "SHORT"} or out.empty:
        return {"passed": False, "kind": "none", "rank": 0}

    current = out.iloc[-1]
    atr14 = current["atr14"]
    volume_average = current["vol_ma20_prev"]
    if (
        math.isfinite(atr14)
        and math.isfinite(volume_average)
        and _body(current) >= BREAKOUT_BODY_ATR * atr14
        and current["v"] >= BREAKOUT_VOLUME_RATIO * volume_average
    ):
        if (direction == "LONG" and current["c"] > current["o"]) or (
            direction == "SHORT" and current["c"] < current["o"]
        ):
            return _result("strong_momentum", 3)
        return {"passed": False, "kind": "none", "rank": 0}

    if len(out) >= 3:
        first, middle, third = out.iloc[-3], out.iloc[-2], current
        if direction == "LONG" and _morning_star(first, middle, third):
            return _result("morning_star", 2)
        if direction == "SHORT" and _evening_star(first, middle, third):
            return _result("evening_star", 2)

    if len(out) >= 2:
        previous = out.iloc[-2]
        if direction == "LONG" and _bullish_engulfing(previous, current):
            return _result("bullish_engulfing", 2)
        if direction == "SHORT" and _bearish_engulfing(previous, current):
            return _result("bearish_engulfing", 2)

    if direction == "LONG" and _hammer(current):
        return _result("hammer", 2)
    if direction == "SHORT" and _shooting_star(current):
        return _result("shooting_star", 2)

    if len(out) >= 3:
        previous, candidate, current = out.iloc[-3], out.iloc[-2], current
        if direction == "LONG" and candidate["l"] < previous["l"] and candidate["l"] < current["l"]:
            return _result("bottom_fractal", 1)
        if direction == "SHORT" and candidate["h"] > previous["h"] and candidate["h"] > current["h"]:
            return _result("top_fractal", 1)

    return {"passed": False, "kind": "none", "rank": 0}
