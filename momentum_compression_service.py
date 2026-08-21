"""Binance Futures market scan coordinator for 15 minute compression signals."""

import copy
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from momentum_compression import (
    _pivots,
    _swing_structure,
    add_compression_indicators,
    evaluate_both_sides,
)
from momentum_compression_store import (
    load_compression_state,
    reconcile_structure_scan,
    save_compression_state,
    write_compression_snapshot,
)
from screener import fetch_klines, is_tradfi_or_junk


FUTURES_BASE = "https://fapi.binance.com"
MIN_COMPRESSION_QUOTE_VOLUME = 1_000_000.0
_ELIGIBLE_STATES = {
    "PRE_BREAKOUT", "COMPRESSION_ACTIVE_LONG", "COMPRESSION_ACTIVE_SHORT",
}
_HTF_LIMIT = 80


def fetch_compression_universe(*, get=requests.get) -> tuple[list[str], dict[str, float]]:
    """Return liquid Binance USDT perpetual symbols and their 24h quote volume."""
    exchange = get(f"{FUTURES_BASE}/fapi/v1/exchangeInfo", timeout=10)
    tickers = get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=10)
    exchange.raise_for_status()
    tickers.raise_for_status()
    volumes = {}
    for row in tickers.json():
        try:
            volumes[row["symbol"]] = float(row["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            continue
    symbols = sorted(
        row["symbol"]
        for row in exchange.json()["symbols"]
        if row.get("status") == "TRADING"
        and row.get("contractType") == "PERPETUAL"
        and row.get("quoteAsset") == "USDT"
        and volumes.get(row["symbol"], 0.0) >= MIN_COMPRESSION_QUOTE_VOLUME
        and not is_tradfi_or_junk(row["symbol"])
    )
    return symbols, volumes


def _htf_direction(frame: pd.DataFrame, side: str) -> bool | None:
    required = {"ot", "o", "h", "l", "c", "v"}
    if not isinstance(frame, pd.DataFrame) or len(frame) < 5 or not required.issubset(frame):
        return None
    try:
        indicators = add_compression_indicators(frame.loc[:, ["ot", "o", "h", "l", "c", "v"]])
        if not pd.notna(indicators[["ema8", "ema21"]].to_numpy()).all():
            return None
        pivots_high, pivots_low = _pivots(indicators, 2)
        swing = _swing_structure(indicators, pivots_high, pivots_low, side)
        last = indicators.iloc[-1]
        if side == "LONG":
            return bool(last["ema8"] > last["ema21"] and last["c"] > max(last["ema8"], last["ema21"]) and swing["valid"])
        return bool(last["ema8"] < last["ema21"] and last["c"] < min(last["ema8"], last["ema21"]) and swing["valid"])
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_htf_alignment(symbol: str, side: str, *, fetch=fetch_klines) -> dict:
    """Confirm the requested direction independently on closed 1h and 4h candles."""
    verdicts = {}
    for interval in ("1h", "4h"):
        try:
            frame = fetch(
                symbol, interval, _HTF_LIMIT, exchange="binance", closed_only=True,
                market_type="futures", testnet=False,
            )
        except Exception:
            frame = None
        verdicts[interval] = _htf_direction(frame, side)
    if any(verdict is None for verdict in verdicts.values()):
        alignment = "UNKNOWN"
    elif all(verdicts.values()):
        alignment = "CONFIRMED"
    else:
        alignment = "CONFLICT"
    return {"alignment": alignment, "timeframes": verdicts}


def _scan_symbol(symbol: str) -> tuple[list[dict], dict[str, pd.DataFrame], int]:
    frame = fetch_klines(
        symbol, "15m", 220, exchange="binance", closed_only=True,
        market_type="futures", testnet=False,
    )
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("15m candle history is unavailable")
    close_ms = int(frame["ot"].iloc[-1]) + 900_000
    price = float(frame["c"].iloc[-1])
    evaluations = evaluate_both_sides(
        symbol, frame, price, evaluated_at_ms=close_ms,
        htf_alignment_by_side={"LONG": "UNKNOWN", "SHORT": "UNKNOWN"},
    )
    for evaluation in evaluations:
        if evaluation["state"] in _ELIGIBLE_STATES:
            evaluation["htf_alignment"] = evaluate_htf_alignment(symbol, evaluation["side"])["alignment"]
    return evaluations, {row["compression_id"]: frame for row in evaluations if row["state"] in _ELIGIBLE_STATES}, close_ms


def _mark_unavailable(state: dict, symbol: str) -> None:
    for item in state["pool"].values():
        if item.get("symbol") == symbol:
            item["data_status"] = "unavailable"


def scan_compression_market(
    state_path: Path,
    snapshot_dir: Path,
    *,
    progress=None,
    max_workers: int = 12,
) -> dict:
    """Scan the liquid Binance Futures universe and atomically persist successful work."""
    state = load_compression_state(state_path)
    symbols, _ = fetch_compression_universe()
    now_ms = int(time.time() * 1000)
    evaluations, frames = [], {}
    errors, close_times = 0, []
    workers = min(12, max(1, max_workers))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_scan_symbol, symbol): symbol for symbol in symbols}
        for completed, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            try:
                rows, symbol_frames, close_ms = future.result()
            except Exception:
                errors += 1
                _mark_unavailable(state, symbol)
            else:
                evaluations.extend(rows)
                frames.update(symbol_frames)
                close_times.append(close_ms)
            if progress is not None:
                progress(completed, len(symbols))
    state, reconciliation = reconcile_structure_scan(state, evaluations, now_ms)
    for row in evaluations:
        if row["state"] in _ELIGIBLE_STATES:
            write_compression_snapshot(snapshot_dir, row, frames[row["compression_id"]])
    state["last_closed_15m_close_time"] = max(
        close_times, default=state["last_closed_15m_close_time"]
    )
    state["last_error"] = f"{errors} symbol scan failures" if errors else ""
    save_compression_state(state_path, state)
    eligible_rows = [row for row in evaluations if row["state"] in _ELIGIBLE_STATES]
    rejection_counts = {}
    for row in evaluations:
        if row["state"] == "REJECTED":
            for reason in row["rejection_reasons"]:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    return {
        "rows": eligible_rows,
        "events": reconciliation["fresh_events"],
        "scanned": len(symbols),
        "eligible": len(eligible_rows),
        "pool_size": len(state["pool"]),
        "errors": errors,
        "rejection_counts": rejection_counts,
        "evaluated_at": now_ms,
        "last_closed_15m_close_time": state["last_closed_15m_close_time"],
    }
