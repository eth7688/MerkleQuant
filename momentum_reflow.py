from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from screener import (
    MIN_PRICE,
    fetch_klines,
    fetch_klines_range,
)

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
BITGET_BASE = "https://api.bitget.com"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"
BITGET_LEDGER_SOURCE = "bitget_usdt_futures"
HOUR_MS = 3_600_000
REFLOW_MIN_VOLUME_USDT = 500_000
STABLE_BASE_ASSETS = {
    "USDC", "FDUSD", "USD1", "RLUSD", "TUSD", "DAI", "USDP", "USDD",
    "PYUSD", "USDY", "CRVUSD", "SUSD", "EUSD", "GHO", "LUSD", "MIM",
    "FRAX", "USTC", "USDE", "USR", "EURS", "EURC", "XSGD", "USDJ",
    "USDX", "USDB", "USDZ", "AEUR", "USDF", "STUSD", "USDQ", "XUSD",
    "USDS",
}
LEVERAGED_MARKERS = ("BULL", "BEAR", "UP", "DOWN")
COMMODITY_BASE_ASSETS = {"XAU", "XAG", "XAUT", "PAXG", "WTI", "BRENT"}
FX_BASE_ASSETS = {
    "EUR", "GBP", "AUD", "JPY", "CAD", "CHF", "NZD", "TRY", "BRL",
    "ZAR", "RUB", "UAH", "PLN", "RON", "ARS",
}


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
        "breakout_close_time": int(row["ot"]) + HOUR_MS,
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
        "breakout_time": event["breakout_open_time"],
        "breakout_open_time": event["breakout_open_time"],
        "breakout_close_time": event.get("breakout_close_time"),
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
            event["max_expansion_atr"] = max(
                event["max_expansion_atr"],
                _expansion_atr(row, event["direction"]),
            )
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
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION or not isinstance(data.get("symbols"), dict):
        raise ValueError("momentum reflow ledger version or shape is invalid")
    return data


def prepare_bitget_ledger(ledger: dict) -> dict:
    if ledger.get("source") == BITGET_LEDGER_SOURCE:
        return ledger
    return {
        "version": LEDGER_VERSION,
        "source": BITGET_LEDGER_SOURCE,
        "symbols": {},
    }


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


def classify_bitget_contract(row: dict) -> str | None:
    base = str(row.get("baseCoin", "")).upper()
    if (
        row.get("quoteCoin") != "USDT"
        or row.get("symbolType") != "perpetual"
        or row.get("symbolStatus") != "normal"
    ):
        return None
    if base in STABLE_BASE_ASSETS or base.endswith(LEVERAGED_MARKERS):
        return None
    if str(row.get("isRwa", "NO")).upper() != "YES":
        return "CRYPTO"
    if base in COMMODITY_BASE_ASSETS:
        return "COMMODITY"
    if base in FX_BASE_ASSETS:
        return "FX"
    return None


def fetch_futures_universe() -> tuple[list[str], dict[str, float], dict[str, str]]:
    contracts_response = requests.get(
        f"{BITGET_BASE}/api/v2/mix/market/contracts",
        params={"productType": BITGET_PRODUCT_TYPE},
        timeout=10,
    )
    contracts_response.raise_for_status()
    tickers_response = requests.get(
        f"{BITGET_BASE}/api/v2/mix/market/tickers",
        params={"productType": BITGET_PRODUCT_TYPE},
        timeout=10,
    )
    tickers_response.raise_for_status()
    volume = {}
    for row in tickers_response.json().get("data", []):
        raw_volume = row.get("usdtVolume")
        if raw_volume is None:
            raw_volume = row.get("quoteVolume")
        if not row.get("symbol") or raw_volume is None:
            continue
        try:
            volume[row["symbol"]] = float(raw_volume)
        except (TypeError, ValueError):
            continue
    instrument_types = {
        row["symbol"]: instrument_type
        for row in contracts_response.json().get("data", [])
        if (instrument_type := classify_bitget_contract(row)) is not None
    }
    symbols = [
        symbol
        for symbol in instrument_types
        if volume.get(symbol, 0.0) >= REFLOW_MIN_VOLUME_USDT
    ]
    return symbols, volume, instrument_types


def _validate_hourly_history(
    frame: pd.DataFrame, cursor: int | None = None
) -> pd.DataFrame:
    required = {"ot", "o", "h", "l", "c", "v"}
    if not isinstance(frame, pd.DataFrame) or frame.empty or not required.issubset(frame):
        raise ValueError("hourly candle history is unavailable")
    open_times = pd.to_numeric(frame["ot"], errors="raise")
    if (open_times.diff().iloc[1:] != HOUR_MS).any():
        raise ValueError("hourly candle history is incomplete")
    if cursor is not None:
        newer = open_times[open_times > cursor]
        if not newer.empty and int(newer.iloc[0]) != cursor + HOUR_MS:
            raise ValueError("hourly candle history has a leading gap")
    return frame.copy()


def _active_ledger_symbols(ledger: dict) -> set[str]:
    terminal = {"CONSUMED", "INVALIDATED"}
    return {
        symbol
        for symbol, state in ledger["symbols"].items()
        if isinstance(state.get("event"), dict)
        and state["event"].get("state") not in terminal
    }


def _scan_symbol(
    symbol: str, old_state: dict, existing: bool
) -> tuple[dict, dict | None, bool, bool]:
    if existing:
        cursor = int(old_state.get("last_processed_open_time", -1))
        hourly = fetch_klines_range(
            symbol,
            "1h",
            max(0, cursor - 1_000 * HOUR_MS),
            exchange="bitget",
            market_type="futures",
            testnet=False,
        )
        if hourly is None:
            hourly = fetch_klines(
                symbol,
                "1h",
                1000,
                exchange="bitget",
                closed_only=True,
                market_type="futures",
                testnet=False,
            )
    else:
        hourly = fetch_klines(
            symbol,
            "1h",
            1000,
            exchange="bitget",
            closed_only=True,
            market_type="futures",
            testnet=False,
        )

    hourly = _validate_hourly_history(
        hourly, cursor if existing else None
    ).sort_values("ot").reset_index(drop=True)
    latest_price = float(hourly.iloc[-1]["c"])
    if not existing and latest_price < MIN_PRICE:
        return old_state, None, False, False

    indicated = add_hourly_indicators(hourly)
    indicated = indicated[indicated.apply(_indicator_row_is_finite, axis=1)].reset_index(drop=True)
    if indicated.empty:
        return old_state, None, False, False
    proposed_state, candidate = advance_symbol(symbol, old_state, indicated)
    if candidate is None:
        return proposed_state, None, not existing, False

    try:
        daily = fetch_klines(
            symbol,
            "1d",
            40,
            exchange="bitget",
            closed_only=True,
            market_type="futures",
            testnet=False,
        )
        if not isinstance(daily, pd.DataFrame) or daily.empty:
            raise ValueError("daily candle history is unavailable")
        confirmation = daily_confirmation(daily, candidate["direction"])
    except Exception:
        return old_state, None, False, True
    if not confirmation["passed"]:
        return proposed_state, None, not existing, False

    candidate_time = candidate["return_open_time"]
    current = indicated[indicated["ot"] == candidate_time]
    if current.empty:
        raise ValueError("candidate candle is outside hourly context")
    row = current.iloc[-1]
    event = proposed_state["event"]
    return (
        proposed_state,
        {
            "symbol": symbol,
            "direction": candidate["direction"],
            "price": float(row["c"]),
            "ema50": float(row["ema50"]),
            "close_distance_atr": float(_close_distance_atr(row)),
            "window_index": int(candidate["window_index"]),
            "breakout_time": int(event["breakout_open_time"]),
            "breakout_close_time": event.get("breakout_close_time"),
            "return_open_time": int(candidate["return_open_time"]),
            "max_expansion_atr": float(event["max_expansion_atr"]),
            "daily_kind": confirmation["kind"],
            "daily_rank": int(confirmation["rank"]),
            "breakout_volume_ratio": float(event["breakout_volume_ratio"]),
        },
        not existing,
        False,
    )


def scan_momentum_reflow(
    ledger_path: Path,
    progress: Callable[[int, int], None] | None = None,
    max_workers: int = 12,
) -> dict:
    ledger = prepare_bitget_ledger(load_ledger(ledger_path))
    eligible_symbols, _, instrument_types = fetch_futures_universe()
    symbols = sorted(set(eligible_symbols) | _active_ledger_symbols(ledger))
    rows: list[dict] = []
    errors = 0
    initialized = 0
    workers = min(3, max(1, max_workers))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _scan_symbol,
                symbol,
                copy.deepcopy(ledger["symbols"].get(symbol, {})),
                symbol in ledger["symbols"],
            ): symbol
            for symbol in symbols
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            try:
                proposed_state, candidate, was_initialized, worker_error = future.result()
            except Exception:
                errors += 1
            else:
                if worker_error:
                    errors += 1
                if was_initialized:
                    initialized += 1
                if proposed_state:
                    ledger["symbols"][symbol] = proposed_state
                if candidate is not None:
                    candidate["instrument_type"] = instrument_types.get(symbol, "CRYPTO")
                    rows.append(candidate)
            if progress is not None:
                progress(completed, len(symbols))

    rows.sort(
        key=lambda row: (
            abs(row["close_distance_atr"]),
            -row["daily_rank"],
            -row["breakout_volume_ratio"],
        )
    )
    save_ledger(ledger_path, ledger)
    return {
        "rows": rows[:80],
        "scanned": len(symbols),
        "errors": errors,
        "initialized": initialized,
    }
