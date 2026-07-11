import json
import math
import os
import shutil
import sys
import time

from trader import BinanceClient, TradeConfig


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def backup(path, stamp):
    if os.path.exists(path):
        copy = f"{path}.bak_orphan_repair_{stamp}"
        shutil.copy2(path, copy)
        return copy
    return ""


def client_for(cfg):
    key = cfg.testnet_api_key or cfg.api_key if cfg.testnet else cfg.api_key
    secret = cfg.testnet_api_secret or cfg.api_secret if cfg.testnet else cfg.api_secret
    if not key or not secret:
        raise RuntimeError("api key/secret missing")
    return BinanceClient(key, secret, testnet=cfg.testnet, market_type="futures")


def find_live_position(client, symbol):
    for p in client.get_positions() or []:
        if p.get("symbol") == symbol:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt != 0:
                return p
    return None


def latest_entry_event(events, symbol):
    for row in reversed(events):
        if row.get("event") == "entry_filled" and row.get("symbol") == symbol:
            return row
    return {}


def find_false_close_index(trades, symbol, live_qty):
    best = -1
    for i, row in enumerate(trades):
        if row.get("symbol") != symbol:
            continue
        reason = str(row.get("reason", "") or "")
        if "减仓" in reason:
            continue
        qty = float(row.get("quantity", 0) or 0)
        qty_match = live_qty > 0 and math.isclose(qty, live_qty, rel_tol=0.02, abs_tol=max(live_qty * 0.02, 1e-8))
        no_exchange_close = not row.get("account_balance_at_exit") and str(row.get("pnl_source", "") or "").startswith("estimate")
        if qty_match and no_exchange_close:
            best = i
    return best


def build_position(symbol, live, entry_event, false_close):
    amt = float(live.get("positionAmt", 0) or 0)
    direction = "LONG" if amt > 0 else "SHORT"
    qty = abs(amt)
    entry = float(live.get("entryPrice", 0) or 0)
    mark = float(live.get("markPrice", 0) or entry)
    unrealized = float(live.get("unRealizedProfit", 0) or 0)

    initial_sl = float(entry_event.get("sl") or entry_event.get("rj_only_stop_price") or 0)
    if not initial_sl:
        initial_sl = entry * (0.98 if direction == "LONG" else 1.02)
    current_sl = float(false_close.get("current_sl") or false_close.get("sl") or initial_sl)
    risk = float(false_close.get("risk") or entry_event.get("risk") or qty * abs(entry - initial_sl))
    entry_time = str(entry_event.get("time") or false_close.get("time") or "")

    pos = {
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry,
        "quantity": qty,
        "sl_price": initial_sl,
        "current_sl": current_sl,
        "risk_usdt": risk,
        "signal_score": float(entry_event.get("score") or false_close.get("score") or 0),
        "entry_time": entry_time,
        "breakeven_triggered": bool(false_close.get("breakeven") or (direction == "LONG" and current_sl >= entry) or (direction == "SHORT" and current_sl <= entry)),
        "breakeven_cooldown": 0,
        "partial_tp_triggered": True if false_close.get("partial") else False,
        "initial_sl": initial_sl,
        "initial_band_hi": entry * 1.02,
        "initial_band_lo": entry * 0.98,
        "highest_price": max(mark, entry),
        "lowest_price": min(mark, entry),
        "tracking_no": "",
        "source_interval": str(entry_event.get("source_interval") or false_close.get("interval") or "30m"),
        "max_favorable_r": float(false_close.get("mfe_r") or 0),
        "max_adverse_r": float(false_close.get("mae_r") or 0),
        "time_stop_armed": True,
        "time_stop_armed_at": entry_time,
        "btc_regime_fields": {k: v for k, v in {**entry_event, **false_close}.items() if str(k).startswith("btc_")},
        "signal_key": str(entry_event.get("signal_key") or false_close.get("signal_key") or ""),
        "target_zone_type": str(entry_event.get("target_zone_type") or false_close.get("target_zone_type") or ""),
        "target_zone_price": float(entry_event.get("target_zone_price") or false_close.get("target_zone_price") or 0),
        "target_zone_low": float(entry_event.get("target_zone_low") or false_close.get("target_zone_low") or 0),
        "target_zone_high": float(entry_event.get("target_zone_high") or false_close.get("target_zone_high") or 0),
        "target_r": float(entry_event.get("target_r") or false_close.get("target_r") or 0),
        "target_distance_pct": float(entry_event.get("target_distance_pct") or false_close.get("target_distance_pct") or 0),
        "target_zone_bars_ago": int(entry_event.get("target_zone_bars_ago") or false_close.get("target_zone_bars_ago") or 0),
        "active_stop_id": "",
    }
    pos["_audit_unrealized_pnl"] = unrealized
    return pos


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "UNIUSDT"
    cfg_path = sys.argv[2] if len(sys.argv) > 2 else "demo_bot_config.json"
    positions_path = sys.argv[3] if len(sys.argv) > 3 else "positions_<uid>.json"
    trades_path = sys.argv[4] if len(sys.argv) > 4 else "trades_<uid>.jsonl"
    events_path = sys.argv[5] if len(sys.argv) > 5 else "signal_events_0.jsonl"

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backups = [backup(positions_path, stamp), backup(trades_path, stamp)]

    cfg = TradeConfig.load(cfg_path)
    live = find_live_position(client_for(cfg), symbol)
    if not live:
        raise RuntimeError(f"{symbol} not found on futures exchange")
    live_qty = abs(float(live.get("positionAmt", 0) or 0))

    positions = load_json(positions_path, [])
    events = load_jsonl(events_path)
    trades = load_jsonl(trades_path)
    entry = latest_entry_event(events, symbol)
    false_idx = find_false_close_index(trades, symbol, live_qty)
    false_close = trades[false_idx] if false_idx >= 0 else {}

    restored = build_position(symbol, live, entry, false_close)
    positions = [p for p in positions if p.get("symbol") != symbol]
    clean_pos = dict(restored)
    clean_pos.pop("_audit_unrealized_pnl", None)
    positions.append(clean_pos)
    with open(positions_path, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)

    voided = None
    if false_idx >= 0:
        voided = trades.pop(false_idx)
        voided["voided"] = True
        voided["void_reason"] = "exchange_position_still_open_after_api_missing_local_exit"
        voided["voided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        with open("trades_0_voided.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(voided, ensure_ascii=False, separators=(",", ":")) + "\n")
        write_jsonl(trades_path, trades)

    print(json.dumps({
        "ok": True,
        "symbol": symbol,
        "backups": [b for b in backups if b],
        "restored_position": {
            "symbol": clean_pos["symbol"],
            "direction": clean_pos["direction"],
            "entry_price": clean_pos["entry_price"],
            "quantity": clean_pos["quantity"],
            "current_sl": clean_pos["current_sl"],
            "breakeven_triggered": clean_pos["breakeven_triggered"],
            "partial_tp_triggered": clean_pos["partial_tp_triggered"],
            "exchange_unrealized_pnl": restored.get("_audit_unrealized_pnl"),
        },
        "voided_trade_time": voided.get("time") if voided else "",
        "voided_trade_pnl": voided.get("pnl") if voided else None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
