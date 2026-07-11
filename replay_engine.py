"""
AXIOM Quant offline replay helper.

Usage:
  python replay_engine.py --csv data.csv --symbol BTCUSDT --interval 15m --out replay_trades.jsonl

CSV columns required: ot,o,h,l,c,v
The replay reuses screener.scan_squeeze_breakout by monkeypatching the data source
to one symbol and a rolling historical window. It is intentionally conservative:
signals are generated only from already closed rows in the CSV.
"""
import argparse
import contextlib
import io
import json
from pathlib import Path

import pandas as pd

import screener


def _load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"ot", "o", "h", "l", "c", "v"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"CSV缺少字段: {sorted(missing)}")
    for col in ["ot", "o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float if col != "ot" else "int64")
    return df.sort_values("ot").reset_index(drop=True)


def _signal_on_window(symbol: str, interval: str, window: pd.DataFrame, min_score: float):
    orig_pairs = screener.fetch_pairs
    orig_klines = screener.fetch_klines
    try:
        screener.fetch_pairs = lambda exchange=None: ([symbol], {symbol: 100_000_000})
        screener.fetch_klines = lambda *args, **kwargs: window.copy().reset_index(drop=True)
        with contextlib.redirect_stdout(io.StringIO()):
            signals = screener.scan_squeeze_breakout(interval, top_n=20, exchange="offline")
        return [s for s in signals if s.get("symbol") == symbol and s.get("score", 0) >= min_score]
    finally:
        screener.fetch_pairs = orig_pairs
        screener.fetch_klines = orig_klines


def replay(df: pd.DataFrame, symbol: str, interval: str, min_score: float = 60.0):
    trades = []
    open_pos = None
    min_bars = 160

    for i in range(min_bars, len(df) + 1):
        window = df.iloc[max(0, i - 220):i].copy().reset_index(drop=True)
        close = float(window["c"].iloc[-1])

        if open_pos:
            direction = open_pos["direction"]
            risk_per_unit = abs(open_pos["entry"] - open_pos["sl"])
            if risk_per_unit <= 0:
                open_pos = None
                continue
            r_now = ((close - open_pos["entry"]) / risk_per_unit
                     if direction == "LONG"
                     else (open_pos["entry"] - close) / risk_per_unit)
            open_pos["mfe_r"] = max(open_pos["mfe_r"], r_now)
            open_pos["mae_r"] = max(open_pos["mae_r"], -r_now)
            open_pos["bars"] += 1

            exit_reason = None
            if direction == "LONG" and close <= open_pos["sl"]:
                exit_reason = "SL"
            elif direction == "SHORT" and close >= open_pos["sl"]:
                exit_reason = "SL"
            elif open_pos["bars"] >= open_pos["time_stop_bars"] and r_now < 0.6:
                exit_reason = "time_stop"

            if exit_reason:
                trades.append({
                    **open_pos,
                    "exit_time": int(window["ot"].iloc[-1]),
                    "exit": close,
                    "reason": exit_reason,
                    "r": round(r_now, 4),
                    "mfe_r": round(open_pos["mfe_r"], 4),
                    "mae_r": round(open_pos["mae_r"], 4),
                })
                open_pos = None
            continue

        signals = _signal_on_window(symbol, interval, window, min_score)
        ready = [s for s in signals if "分型" in str(s.get("retest", ""))]
        if not ready:
            continue
        sig = sorted(ready, key=lambda x: x.get("score", 0), reverse=True)[0]
        details = screener.verify_pool_signal_details(window, sig["direction"], interval=interval)
        if not details:
            continue
        fractal_sl = details["fractal_sl"] * (0.998 if sig["direction"] == "LONG" else 1.002)
        band_sl = details["band_sl"]
        sl = max(fractal_sl, band_sl) if sig["direction"] == "LONG" else min(fractal_sl, band_sl)
        if (sig["direction"] == "LONG" and sl >= close) or (sig["direction"] == "SHORT" and sl <= close):
            continue
        open_pos = {
            "entry_time": int(window["ot"].iloc[-1]),
            "symbol": symbol,
            "interval": interval,
            "direction": sig["direction"],
            "entry": close,
            "sl": sl,
            "score": sig.get("score", 0),
            "retest": sig.get("retest", ""),
            "bars": 0,
            "time_stop_bars": {"15m": 6, "1h": 5, "4h": 4, "1d": 3}.get(interval, 6),
            "mfe_r": 0.0,
            "mae_r": 0.0,
        }

    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--min-score", type=float, default=60.0)
    ap.add_argument("--out", default="replay_trades.jsonl")
    args = ap.parse_args()

    df = _load_csv(args.csv)
    trades = replay(df, args.symbol.upper(), args.interval, args.min_score)
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"replay trades: {len(trades)} -> {out}")


if __name__ == "__main__":
    main()
