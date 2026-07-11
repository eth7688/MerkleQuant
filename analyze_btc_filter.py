#!/usr/bin/env python3
"""Review BTC direction-filter rejects against later market paths and trade history."""

import argparse
import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


REVIEW_HORIZONS = (6, 12, 24)
BAR_MINUTES = 30


def load_jsonl(path):
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def dedupe_events(events):
    latest = {}
    for event in events:
        key = (
            str(event.get("symbol", "")),
            str(event.get("direction", "")),
            str(event.get("rj_only_key_time", "")),
        )
        if key not in latest or str(event.get("time", "")) > str(latest[key].get("time", "")):
            latest[key] = event
    return sorted(latest.values(), key=lambda item: str(item.get("time", "")))


def load_blocked_events(project_dir):
    specs = (
        ("signal_events_0.jsonl", "binance", "demo"),
        ("signal_events_2.jsonl", "bitget", "auto"),
    )
    events = []
    for filename, exchange, engine in specs:
        for event in load_jsonl(project_dir / filename):
            if event.get("event") != "entry_reject" or event.get("reason") != "btc_direction_filter":
                continue
            item = dict(event)
            item["exchange"] = exchange
            item["engine"] = engine
            events.append(item)
    return dedupe_events(events)


def trade_would_be_blocked(trade):
    direction = str(trade.get("direction", "")).upper()
    stage = str(trade.get("btc_stage", "unknown"))
    market_direction = str(trade.get("btc_direction", "unknown"))
    extreme = bool(trade.get("btc_extreme_veto", False))
    reversal = bool(trade.get("btc_coin_reversal_pass", False))
    opposite = (
        direction == "SHORT" and market_direction == "bull"
    ) or (
        direction == "LONG" and market_direction == "bear"
    )
    if not opposite:
        return False
    if extreme and stage in {"early_bull", "mid_bull", "early_bear", "mid_bear"}:
        return True
    return stage in {"early_bull", "mid_bull", "early_bear", "mid_bear"} and not reversal


def event_time_ms(event, offset_hours):
    value = str(event.get("time", "")).replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(value)
    return int(parsed.timestamp() * 1000) - int(float(offset_hours) * 3600 * 1000)


def _get_json(url, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "AXIOM-BTC-Filter-Review/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_candles(event, start_ms, end_ms):
    symbol = str(event.get("symbol", ""))
    if event.get("exchange") == "binance":
        data = _get_json("https://fapi.binance.com/fapi/v1/klines", {
            "symbol": symbol,
            "interval": "1m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        })
        return [[int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])] for row in data]

    data = _get_json("https://api.bitget.com/api/v2/mix/market/candles", {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "granularity": "1m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    })
    if str(data.get("code")) != "00000":
        raise RuntimeError(str(data)[:300])
    rows = [[int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])] for row in data.get("data", [])]
    return sorted(rows, key=lambda row: row[0])


def evaluate_path(event, candles):
    entry = float(event.get("entry", 0) or 0)
    stop = float(event.get("sl", 0) or 0)
    distance = abs(entry - stop)
    if entry <= 0 or stop <= 0 or distance <= 0:
        raise ValueError("invalid entry or stop")

    direction = str(event.get("direction", "")).upper()
    one_r = entry + distance if direction == "LONG" else entry - distance
    max_favorable = 0.0
    max_adverse = 0.0
    first_event = "none"

    for _, _, high, low, _ in candles:
        if direction == "LONG":
            favorable = (high - entry) / distance
            adverse = (entry - low) / distance
            hit_one_r = high >= one_r
            hit_stop = low <= stop
        else:
            favorable = (entry - low) / distance
            adverse = (high - entry) / distance
            hit_one_r = low <= one_r
            hit_stop = high >= stop
        max_favorable = max(max_favorable, favorable)
        max_adverse = max(max_adverse, adverse)
        if first_event == "none":
            if hit_one_r and hit_stop:
                first_event = "same_1m_ambiguous"
            elif hit_one_r:
                first_event = "plus_1R"
            elif hit_stop:
                first_event = "stop"

    last_price = float(candles[-1][4]) if candles else entry
    final_r = (last_price - entry) / distance if direction == "LONG" else (entry - last_price) / distance
    target_r = float(event.get("target_r", 0) or 0)
    return {
        "mfe_r": round(max_favorable, 4),
        "mae_r": round(max_adverse, 4),
        "final_r": round(final_r, 4),
        "first_event": first_event,
        "target_reached": bool(target_r > 0 and max_favorable >= target_r),
        "last_price": last_price,
    }


def evaluate_horizons(event, candles, horizons=REVIEW_HORIZONS, bar_minutes=BAR_MINUTES):
    if not candles:
        return {str(value): evaluate_path(event, []) for value in horizons}
    first_ms = int(candles[0][0])
    result = {}
    for bars in horizons:
        cutoff = first_ms + int(bars) * int(bar_minutes) * 60_000
        window = [row for row in candles if int(row[0]) < cutoff]
        result[str(bars)] = evaluate_path(event, window)
    return result


def review_events(events, horizon_minutes, offset_hours, now_ms=None):
    now_ms = int(now_ms or time.time() * 1000)
    reviewed = []
    for event in events:
        item = {
            "engine": event.get("engine"),
            "exchange": event.get("exchange"),
            "time": event.get("time"),
            "symbol": event.get("symbol"),
            "direction": event.get("direction"),
            "entry": event.get("entry"),
            "sl": event.get("sl"),
            "btc_1h": event.get("btc_1h_overall"),
            "btc_4h": event.get("btc_4h_overall"),
        }
        try:
            start_ms = ((event_time_ms(event, offset_hours) // 60000) + 1) * 60000
            horizon_end = start_ms + int(horizon_minutes) * 60000
            end_ms = min(now_ms, horizon_end)
            candles = fetch_candles(event, start_ms, end_ms)
            item.update(evaluate_path(event, candles))
            item["horizons"] = evaluate_horizons(event, candles)
            item["observed_minutes"] = max(0, int((end_ms - start_ms) // 60000))
            item["complete"] = now_ms >= horizon_end
            item["error"] = "" if candles else "no_candles"
        except Exception as exc:
            item.update({"complete": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
        reviewed.append(item)
        time.sleep(0.08)
    return reviewed


def _trade_pnl(trade):
    exchange_pnl = trade.get("exchange_pnl")
    return float(exchange_pnl if exchange_pnl not in (None, "") else trade.get("pnl", 0) or 0)


def _group_stats(trades):
    if not trades:
        return {"count": 0, "wins": 0, "win_rate": 0.0, "pnl": 0.0, "sum_r": 0.0, "avg_r": 0.0}
    pnls = [_trade_pnl(trade) for trade in trades]
    r_values = [float(trade.get("r", 0) or 0) for trade in trades]
    wins = sum(value > 0 for value in pnls)
    return {
        "count": len(trades),
        "wins": wins,
        "win_rate": round(wins * 100 / len(trades), 1),
        "pnl": round(sum(pnls), 2),
        "sum_r": round(sum(r_values), 4),
        "avg_r": round(sum(r_values) / len(r_values), 4),
        "mfe_ge_1r": sum(float(trade.get("mfe_r", 0) or 0) >= 1.0 for trade in trades),
        "mfe_ge_08r": sum(float(trade.get("mfe_r", 0) or 0) >= 0.8 for trade in trades),
    }


def historical_summary(project_dir):
    result = {}
    for filename, engine in (("trades_<uid>.jsonl", "demo"), ("trades_<uid>.jsonl", "auto")):
        trades = [
            trade for trade in load_jsonl(project_dir / filename)
            if not trade.get("voided")
            and trade.get("source_strategy") == "rj_only"
            and trade.get("btc_1h_overall")
            and trade.get("btc_4h_overall")
        ]
        result[engine] = {
            "would_block": _group_stats([trade for trade in trades if trade_would_be_blocked(trade)]),
            "would_allow": _group_stats([trade for trade in trades if not trade_would_be_blocked(trade)]),
        }
    return result


def build_summary(reviewed, min_samples):
    valid = [item for item in reviewed if not item.get("error")]
    complete = [item for item in valid if item.get("complete")]
    ready = len(complete) >= int(min_samples)
    return {
        "unique_rejects": len(reviewed),
        "valid_paths": len(valid),
        "complete_paths": len(complete),
        "incomplete_paths": len(valid) - len(complete),
        "errors": len(reviewed) - len(valid),
        "plus_1r_first": sum(item.get("first_event") == "plus_1R" for item in complete),
        "stop_first": sum(item.get("first_event") == "stop" for item in complete),
        "ambiguous": sum(item.get("first_event") == "same_1m_ambiguous" for item in complete),
        "unresolved": sum(item.get("first_event") == "none" for item in complete),
        "sample_ready": ready,
        "status": "READY" if ready else "SAMPLE_NOT_READY",
        "minimum_samples": int(min_samples),
    }


def print_report(report):
    summary = report["summary"]
    print("BTC direction filter counterfactual review")
    print(
        f"Unique rejects: {summary['unique_rejects']} | Complete: {summary['complete_paths']} | "
        f"Incomplete: {summary['incomplete_paths']} | Ready: {summary['sample_ready']} "
        f"(minimum {summary['minimum_samples']})"
    )
    if not summary["sample_ready"]:
        print("SAMPLE_NOT_READY: no filter conclusion is allowed yet")
    print("symbol\tengine\tminutes\tMFE_R\tMAE_R\tfinal_R\tfirst\tcomplete")
    for item in report["events"]:
        if item.get("error"):
            print(f"{item.get('symbol')}\t{item.get('engine')}\tERROR\t{item.get('error')}")
            continue
        print(
            f"{item.get('symbol')}\t{item.get('engine')}\t{item.get('observed_minutes', 0)}\t"
            f"{item.get('mfe_r', 0):.3f}\t{item.get('mae_r', 0):.3f}\t"
            f"{item.get('final_r', 0):.3f}\t{item.get('first_event')}\t{item.get('complete')}"
        )
    print("Historical RJ comparison:")
    for engine, groups in report["history"].items():
        blocked = groups["would_block"]
        allowed = groups["would_allow"]
        print(
            f"{engine}: block n={blocked['count']} win={blocked['win_rate']}% pnl={blocked['pnl']} "
            f"sumR={blocked['sum_r']} | allow n={allowed['count']} win={allowed['win_rate']}% "
            f"pnl={allowed['pnl']} sumR={allowed['sum_r']}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--horizon-bars", type=int, default=24)
    parser.add_argument("--bar-minutes", type=int, default=30)
    parser.add_argument("--min-samples", type=int, default=50)
    parser.add_argument("--event-time-offset-hours", type=float, default=8.0)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    events = load_blocked_events(project_dir)
    reviewed = review_events(
        events,
        horizon_minutes=max(1, args.horizon_bars * args.bar_minutes),
        offset_hours=args.event_time_offset_hours,
    )
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "horizon_bars": args.horizon_bars,
        "bar_minutes": args.bar_minutes,
        "summary": build_summary(reviewed, args.min_samples),
        "events": reviewed,
        "history": historical_summary(project_dir),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    if args.json_output:
        print(rendered)
    else:
        print_report(report)


if __name__ == "__main__":
    main()
