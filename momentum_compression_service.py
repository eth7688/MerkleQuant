"""Binance Futures market scan coordinator for 15 minute compression signals."""

import copy
import math
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
    compression_state_lock,
    load_compression_state,
    effective_compression_id,
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


def fetch_live_price(symbol: str, *, get=requests.get) -> float:
    response = get(f"{FUTURES_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    response.raise_for_status()
    price = float(response.json()["price"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("ticker price is unavailable")
    return price


def fetch_live_prices(*, get=requests.get) -> dict[str, float]:
    """Return the finite positive prices from Binance's bulk futures ticker."""
    response = get(f"{FUTURES_BASE}/fapi/v1/ticker/price", timeout=10)
    response.raise_for_status()
    prices = {}
    for row in response.json():
        try:
            price = float(row["price"])
            if math.isfinite(price) and price > 0:
                prices[row["symbol"]] = price
        except (KeyError, TypeError, ValueError):
            continue
    return prices


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


def _htf_direction(frame: pd.DataFrame, side: str) -> str:
    required = {"ot", "o", "h", "l", "c", "v"}
    if not isinstance(frame, pd.DataFrame) or len(frame) < 5 or not required.issubset(frame):
        return "UNKNOWN"
    try:
        indicators = add_compression_indicators(frame.loc[:, ["ot", "o", "h", "l", "c", "v"]])
        if not pd.notna(indicators[["ema8", "ema21"]].to_numpy()).all():
            return "UNKNOWN"
        pivots_high, pivots_low = _pivots(indicators, 2)
        swing = _swing_structure(indicators, pivots_high, pivots_low, side)
        last = indicators.iloc[-1]
        if side == "LONG":
            aligned = last["ema8"] > last["ema21"] and last["c"] > max(last["ema8"], last["ema21"])
            opposite = last["ema8"] < last["ema21"] and last["c"] < min(last["ema8"], last["ema21"])
        else:
            aligned = last["ema8"] < last["ema21"] and last["c"] < min(last["ema8"], last["ema21"])
            opposite = last["ema8"] > last["ema21"] and last["c"] > max(last["ema8"], last["ema21"])
        if aligned and swing["valid"]:
            return "ALIGNED"
        opposite_swing = _swing_structure(indicators, pivots_high, pivots_low, "SHORT" if side == "LONG" else "LONG")
        return "OPPOSITE" if opposite and opposite_swing["valid"] else "UNKNOWN"
    except (KeyError, TypeError, ValueError):
        return "UNKNOWN"


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
    if all(verdict == "ALIGNED" for verdict in verdicts.values()):
        alignment = "CONFIRMED"
    elif any(verdict == "OPPOSITE" for verdict in verdicts.values()):
        alignment = "CONFLICT"
    else:
        alignment = "UNKNOWN"
    return {"alignment": alignment, "timeframes": verdicts}


def _scan_symbol(symbol: str, *, live_price: float, evaluated_at_ms: int) -> tuple[list[dict], dict[str, pd.DataFrame], int]:
    frame = fetch_klines(
        symbol, "15m", 220, exchange="binance", closed_only=True,
        market_type="futures", testnet=False,
    )
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("15m candle history is unavailable")
    evaluations = evaluate_both_sides(
        symbol, frame, live_price, evaluated_at_ms=evaluated_at_ms,
        htf_alignment_by_side={"LONG": "UNKNOWN", "SHORT": "UNKNOWN"},
    )
    for evaluation in evaluations:
        if evaluation["state"] in _ELIGIBLE_STATES:
            htf = evaluate_htf_alignment(symbol, evaluation["side"])
            evaluation["htf_alignment"] = htf["alignment"]
            evaluation["htf_timeframes"] = htf.get("timeframes", {"1h": None, "4h": None})
        if evaluation["state"] not in _ELIGIBLE_STATES:
            evaluation["htf_timeframes"] = {"1h": None, "4h": None}
    return evaluations, {symbol: frame}, int(frame["ot"].iloc[-1]) + 900_000


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
    started_at = int(time.time() * 1000)
    symbols, _ = fetch_compression_universe()
    live_prices = fetch_live_prices()
    now_ms = int(time.time() * 1000)
    evaluations, frames = [], {}
    errors, close_times, failed_symbols = 0, [], []
    price_missing = [symbol for symbol in symbols if symbol not in live_prices]
    failed_symbols.extend(price_missing)
    errors += len(price_missing)
    symbols_to_scan = [symbol for symbol in symbols if symbol in live_prices]
    workers = min(12, max(1, max_workers))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_scan_symbol, symbol, live_price=live_prices[symbol], evaluated_at_ms=now_ms): symbol
            for symbol in symbols_to_scan
        }
        for completed, future in enumerate(as_completed(futures), start=len(price_missing) + 1):
            symbol = futures[future]
            try:
                rows, symbol_frames, close_ms = future.result()
            except Exception:
                errors += 1
                failed_symbols.append(symbol)
            else:
                evaluations.extend(rows)
                frames.update(symbol_frames)
                close_times.append(close_ms)
            if progress is not None:
                progress(completed, len(symbols))
    # Reload only for the short persistence transaction: live prices may have
    # advanced while network-bound discovery was in progress.
    with compression_state_lock(state_path):
        state = load_compression_state(state_path)
        for symbol in failed_symbols:
            _mark_unavailable(state, symbol)
        for row in evaluations:
            if row["state"] not in _ELIGIBLE_STATES:
                continue
            row["compression_id"] = effective_compression_id(state, row)
            prior = state["pool"].get(row["compression_id"])
            if prior and prior.get("ohlcv_snapshot_ref"):
                row["ohlcv_snapshot_ref"] = prior["ohlcv_snapshot_ref"]
                continue
            selected = frames[row["symbol"]]
            selected = selected[(selected["ot"] >= row["compression_start_time"]) & (selected["ot"] <= row["compression_end_time"])].reset_index(drop=True)
            row["ohlcv_snapshot_ref"] = write_compression_snapshot(snapshot_dir, row, selected)
        state, reconciliation = reconcile_structure_scan(
            state, evaluations, now_ms, successful_symbols=set(symbols) - set(failed_symbols),
        )
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
    finished_at = int(time.time() * 1000)
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
        "failed_symbols": failed_symbols,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": finished_at - started_at,
    }
