from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from pathlib import Path

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
    current_range = float(current["h"]) - float(current["l"])
    if (
        math.isfinite(atr14)
        and math.isfinite(volume_average)
        and math.isfinite(current_range)
        and current_range > 0.0
        and _body(current) >= BREAKOUT_BODY_ATR * atr14
        and _body(current) / current_range >= 0.65
        and current["v"] >= BREAKOUT_VOLUME_RATIO * volume_average
    ):
        if (direction == "LONG" and current["c"] > current["o"]) or (
            direction == "SHORT" and current["c"] < current["o"]
        ):
            return _result("strong_momentum", 3)

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


def _touches_zone(row) -> bool:
    lower = row["ema50"] - TOUCH_ZONE_ATR * row["atr14"]
    upper = row["ema50"] + TOUCH_ZONE_ATR * row["atr14"]
    return row["l"] <= upper and row["h"] >= lower


def _close_distance_atr(row) -> float:
    return abs(row["c"] - row["ema50"]) / row["atr14"]


def _slope_aligned(frame: pd.DataFrame, index: int, direction: str) -> bool | None:
    if index < EMA_SLOPE_BARS:
        return None
    now = frame.iloc[index]["ema50"]
    prior = frame.iloc[index - EMA_SLOPE_BARS]["ema50"]
    return now > prior if direction == "LONG" else now < prior


def _breakout_direction(previous, current) -> str | None:
    body_ratio = abs(current["c"] - current["o"]) / current["atr14"]
    volume_ratio = current["v"] / current["vol_ma20_prev"]
    if body_ratio < BREAKOUT_BODY_ATR or volume_ratio < BREAKOUT_VOLUME_RATIO:
        return None
    if previous["c"] <= previous["ema50"] and current["c"] > current["ema50"]:
        return "LONG"
    if previous["c"] >= previous["ema50"] and current["c"] < current["ema50"]:
        return "SHORT"
    return None


def _indicator_row_is_finite(row) -> bool:
    return all(
        math.isfinite(float(row[column]))
        for column in ("ema50", "atr14", "vol_ma20_prev")
    ) and float(row["atr14"]) > 0.0 and float(row["vol_ma20_prev"]) > 0.0


def _new_event(direction: str, row) -> dict:
    return {
        "direction": direction,
        "state": "WAIT_EXPANSION",
        "breakout_open_time": int(row["ot"]),
        "breakout_volume_ratio": float(row["v"] / row["vol_ma20_prev"]),
        "expansion_time": 0,
        "max_expansion_atr": 0.0,
        "first_touch_time": 0,
        "return_window_index": 0,
        "audit_reason": "breakout_detected",
        "expansion_followups": 0,
    }


def _expansion_atr(row, direction: str) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    return sign * (float(row["c"]) - float(row["ema50"])) / float(row["atr14"])


def _candidate(symbol: str, event: dict) -> dict:
    return {
        "symbol": symbol,
        "direction": event["direction"],
        "breakout_open_time": event["breakout_open_time"],
        "expansion_time": event["expansion_time"],
        "return_open_time": event["return_open_time"],
        "window_index": event["return_window_index"],
    }


def _confirm_expansion(event: dict, row) -> None:
    event["state"] = "WAIT_FIRST_RETURN"
    event["expansion_time"] = int(row["ot"])
    event["audit_reason"] = "expansion_confirmed"


def advance_symbol(symbol: str, symbol_state: dict, frame: pd.DataFrame) -> tuple[dict, dict | None]:
    """Advance one symbol's closed-candle event lifecycle without double counting."""
    state = copy.deepcopy(symbol_state) if symbol_state else {}
    state.setdefault("last_processed_open_time", -1)
    state.setdefault("event", None)
    out = frame.sort_values("ot").drop_duplicates("ot", keep="last").reset_index(drop=True)
    cursor = int(state["last_processed_open_time"])
    newest_candidate = None
    processed_any = False

    for index, row in out.iterrows():
        open_time = int(row["ot"])
        if open_time <= cursor:
            continue
        if not _indicator_row_is_finite(row):
            break

        processed_any = True
        newest_candidate = None
        event = state["event"]
        direction = None
        if index > 0 and _indicator_row_is_finite(out.iloc[index - 1]):
            direction = _breakout_direction(out.iloc[index - 1], row)

        if event is None or event["state"] in {"CONSUMED", "INVALIDATED"}:
            if direction is not None:
                event = _new_event(direction, row)
                state["event"] = event
            else:
                cursor = open_time
                continue

        if event["state"] == "WAIT_EXPANSION":
            expansion = _expansion_atr(row, event["direction"])
            event["max_expansion_atr"] = max(event["max_expansion_atr"], expansion)
            if expansion >= EXPANSION_ATR:
                _confirm_expansion(event, row)
                cursor = open_time
                continue
            elif open_time != event["breakout_open_time"]:
                event["expansion_followups"] += 1
                if event["expansion_followups"] >= EXPANSION_FOLLOW_BARS:
                    event["state"] = "INVALIDATED"
                    event["audit_reason"] = "expansion_timeout"

        if event["state"] == "WAIT_FIRST_RETURN":
            slope = _slope_aligned(out, index, event["direction"])
            if slope is not None and not slope:
                event["state"] = "INVALIDATED"
                event["audit_reason"] = "ema_slope_reversal"
            elif _touches_zone(row):
                if _close_distance_atr(row) > CLOSE_DISTANCE_ATR:
                    event["state"] = "CONSUMED"
                    event["audit_reason"] = "first_touch_close_too_far"
                else:
                    event["state"] = "RETURN_WINDOW"
                    event["first_touch_time"] = open_time
                    event["return_window_index"] = 1
                    event["return_open_time"] = open_time
                    event["audit_reason"] = "return_window_open"
                    newest_candidate = _candidate(symbol, event)

        elif event["state"] == "RETURN_WINDOW":
            if not _touches_zone(row) or _close_distance_atr(row) > CLOSE_DISTANCE_ATR:
                event["state"] = "CONSUMED"
                event["audit_reason"] = "return_window_left_zone"
            else:
                event["return_window_index"] += 1
                event["return_open_time"] = open_time
                newest_candidate = _candidate(symbol, event)
                if event["return_window_index"] >= RETURN_WINDOW_BARS:
                    event["state"] = "CONSUMED"
                    event["audit_reason"] = "return_window_complete"

        cursor = open_time

    state["last_processed_open_time"] = cursor
    if not processed_any and state["event"] and state["event"]["state"] == "RETURN_WINDOW":
        return state, _candidate(symbol, state["event"])
    return state, newest_candidate


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"version": LEDGER_VERSION, "symbols": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("momentum reflow ledger is unreadable") from error
    if data.get("version") != LEDGER_VERSION or not isinstance(data.get("symbols"), dict):
        raise ValueError("momentum reflow ledger version or shape is invalid")
    return data


def save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
