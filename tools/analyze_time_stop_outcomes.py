import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


TIME_STOP_RE = re.compile(
    r"(?P<interval>\w+)\s+(?P<bars>\d+)\s*/\s*(?P<need>\d+)K.*?"
    r"当前推进(?P<current>-?\d+(?:\.\d+)?)R<(?P<threshold>-?\d+(?:\.\d+)?)R.*?"
    r"最大推进(?P<mfe>-?\d+(?:\.\d+)?)R"
)


def parse_time(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def read_jsonl(path):
    rows = []
    bad = 0
    if not path.exists():
        return rows, bad
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                bad += 1
    return rows, bad


def fnum(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return default


def extract_timeout(reason):
    text = str(reason or "")
    if "未起爆" not in text:
        return None
    m = TIME_STOP_RE.search(text)
    if not m:
        return {"raw": text}
    return {
        "raw": text,
        "interval": m.group("interval"),
        "bars": int(m.group("bars")),
        "need_bars": int(m.group("need")),
        "current_r": fnum(m.group("current")),
        "threshold_r": fnum(m.group("threshold")),
        "max_favorable_r": fnum(m.group("mfe")),
    }


def trade_timeout_rows(trades):
    out = []
    for row in trades:
        meta = extract_timeout(row.get("reason"))
        if not meta:
            continue
        item = dict(row)
        item["_timeout"] = meta
        item["_source"] = "trade"
        out.append(item)
    return out


def event_timeout_rows(events):
    out = []
    for row in events:
        meta = extract_timeout(row.get("exit_reason") or row.get("reason"))
        if not meta:
            continue
        item = dict(row)
        item["_timeout"] = meta
        item["_source"] = "event"
        out.append(item)
    return out


def dedupe_events(rows):
    by_key = {}
    for row in rows:
        meta = row.get("_timeout") or {}
        key = (
            row.get("symbol"),
            row.get("direction"),
            row.get("entry"),
            row.get("source_interval") or meta.get("interval"),
            meta.get("bars"),
            meta.get("need_bars"),
        )
        cur = by_key.get(key)
        if not cur:
            by_key[key] = dict(row, _repeat_count=1, _first_time=row.get("time"), _last_time=row.get("time"))
            continue
        cur["_repeat_count"] += 1
        cur["_last_time"] = row.get("time")
    return list(by_key.values())


def interval_to_ms(interval):
    table = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }
    return table.get(str(interval or "15m"), 900_000)


def fetch_klines(symbol, interval, start_dt, limit):
    import requests

    start_ms = int(start_dt.timestamp() * 1000) + interval_to_ms(interval)
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "limit": limit,
    }
    url = "https://fapi.binance.com/fapi/v1/klines"
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    rows = []
    for item in resp.json():
        rows.append(
            {
                "open_time": int(item[0]),
                "open": fnum(item[1]),
                "high": fnum(item[2]),
                "low": fnum(item[3]),
                "close": fnum(item[4]),
            }
        )
    return rows


def infer_exit_price(row):
    exit_price = fnum(row.get("exit"), 0.0)
    if exit_price > 0:
        return exit_price
    meta = row.get("_timeout") or {}
    current_r = meta.get("current_r")
    entry = fnum(row.get("entry"), 0.0)
    sl = fnum(row.get("sl") or row.get("current_sl"), 0.0)
    if entry <= 0 or sl <= 0 or current_r is None:
        return 0.0
    risk_unit = abs(entry - sl)
    if risk_unit <= 0:
        return 0.0
    if row.get("direction") == "SHORT":
        return entry - current_r * risk_unit
    return entry + current_r * risk_unit


def risk_unit(row):
    entry = fnum(row.get("entry"), 0.0)
    sl = fnum(row.get("sl") or row.get("current_sl"), 0.0)
    qty = fnum(row.get("quantity") or row.get("qty"), 0.0)
    risk_money = fnum(row.get("risk"), 0.0)
    if qty > 0 and risk_money > 0:
        return risk_money / qty
    if entry > 0 and sl > 0:
        return abs(entry - sl)
    return 0.0


def post_exit_metrics(row, klines):
    exit_price = infer_exit_price(row)
    ru = risk_unit(row)
    if exit_price <= 0 or ru <= 0 or not klines:
        return None
    high = max(k["high"] for k in klines)
    low = min(k["low"] for k in klines)
    final = klines[-1]["close"]
    if row.get("direction") == "SHORT":
        post_mfe = (exit_price - low) / ru
        post_mae = (high - exit_price) / ru
        final_delta = (exit_price - final) / ru
    else:
        post_mfe = (high - exit_price) / ru
        post_mae = (exit_price - low) / ru
        final_delta = (final - exit_price) / ru
    if post_mfe >= 2.0:
        verdict = "大幅截断后续盈利"
    elif post_mfe >= 1.0:
        verdict = "可能截断后续盈利"
    elif post_mfe >= 0.6:
        verdict = "边界偏早"
    elif post_mae > post_mfe and final_delta <= 0:
        verdict = "有效截断风险"
    else:
        verdict = "影响中性"
    return {
        "exit_price": exit_price,
        "risk_unit": ru,
        "post_mfe_r": post_mfe,
        "post_mae_r": post_mae,
        "final_delta_r": final_delta,
        "bars": len(klines),
        "verdict": verdict,
    }


def summarize(rows, title):
    print(f"\n=== {title} ===")
    print(f"count={len(rows)}")
    if not rows:
        return
    by_symbol = Counter(r.get("symbol", "") for r in rows)
    by_event = Counter(r.get("event", r.get("_source", "")) for r in rows)
    by_reason = Counter(r.get("reason", "") for r in rows)
    print("by_event:", by_event.most_common(10))
    print("by_symbol:", by_symbol.most_common(20))
    if by_reason:
        print("by_reason:", by_reason.most_common(10))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--trades", action="append", default=[])
    ap.add_argument("--events", action="append", default=[])
    ap.add_argument("--fetch", action="store_true", help="Fetch post-exit Binance futures klines")
    ap.add_argument("--horizon-bars", type=int, default=24)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    root = Path(args.root)
    trades = []
    events = []
    bad = defaultdict(int)
    for name in args.trades or []:
        rows, nbad = read_jsonl(root / name)
        trades.extend(rows)
        bad[name] += nbad
    for name in args.events or []:
        rows, nbad = read_jsonl(root / name)
        events.extend(rows)
        bad[name] += nbad

    print("=== INPUT ===")
    print(f"root={root.resolve()}")
    print(f"trades={len(trades)} events={len(events)} bad={dict(bad)}")
    if trades:
        print(f"trade_time_range={trades[0].get('time')} -> {trades[-1].get('time')}")
    if events:
        print(f"event_time_range={events[0].get('time')} -> {events[-1].get('time')}")

    trade_timeouts = trade_timeout_rows(trades)
    event_timeouts = event_timeout_rows(events)
    deduped = dedupe_events(event_timeouts)
    summarize(trade_timeouts, "TIME STOP TRADES")
    summarize(event_timeouts, "TIME STOP EVENTS RAW")
    summarize(deduped, "TIME STOP EVENTS DEDUPED")

    if deduped:
        print("\nLatest deduped timeout events:")
        for row in deduped[-12:]:
            meta = row.get("_timeout") or {}
            print(
                "{time} {symbol:12} {direction:5} event={event:12} reason={reason:16} "
                "repeat={rep:3} currentR={cur:.2f} maxR={mfe:.2f} bars={bars}/{need} entry={entry} sl={sl}".format(
                    time=row.get("_last_time") or row.get("time"),
                    symbol=str(row.get("symbol", "")),
                    direction=str(row.get("direction", "")),
                    event=str(row.get("event", "")),
                    reason=str(row.get("reason", ""))[:16],
                    rep=int(row.get("_repeat_count", 1) or 1),
                    cur=fnum(meta.get("current_r")),
                    mfe=fnum(meta.get("max_favorable_r")),
                    bars=meta.get("bars", "-"),
                    need=meta.get("need_bars", "-"),
                    entry=row.get("entry"),
                    sl=row.get("sl") or row.get("current_sl"),
                )
            )

    rows_for_fetch = trade_timeouts if trade_timeouts else deduped
    rows_for_fetch = rows_for_fetch[-args.limit :]
    if not args.fetch:
        print("\nPOST_EXIT: skipped (run with --fetch to pull Binance futures klines)")
        return

    print("\n=== POST EXIT OUTCOME ===")
    verdicts = Counter()
    metric_rows = []
    for row in rows_for_fetch:
        dt = parse_time(row.get("time") or row.get("_last_time"))
        symbol = row.get("symbol")
        interval = row.get("interval") or row.get("source_interval") or (row.get("_timeout") or {}).get("interval") or "15m"
        if not dt or not symbol:
            continue
        try:
            klines = fetch_klines(symbol, interval, dt, args.horizon_bars)
            metric = post_exit_metrics(row, klines)
        except Exception as e:
            print(f"fetch_failed {symbol} {interval} {dt.isoformat()} {e}")
            continue
        if not metric:
            continue
        verdicts[metric["verdict"]] += 1
        metric_rows.append((row, metric))
    print("verdicts:", verdicts.most_common())
    if metric_rows:
        avg_mfe = sum(m["post_mfe_r"] for _, m in metric_rows) / len(metric_rows)
        avg_mae = sum(m["post_mae_r"] for _, m in metric_rows) / len(metric_rows)
        print(f"analyzed={len(metric_rows)} avg_post_mfe_r={avg_mfe:.2f} avg_post_mae_r={avg_mae:.2f}")
        print("top_missed:")
        for row, metric in sorted(metric_rows, key=lambda x: x[1]["post_mfe_r"], reverse=True)[:20]:
            print(
                "{time} {symbol:12} {direction:5} postMFE={mfe:.2f}R postMAE={mae:.2f}R final={final:.2f}R "
                "verdict={verdict} reason={reason}".format(
                    time=row.get("time") or row.get("_last_time"),
                    symbol=str(row.get("symbol", "")),
                    direction=str(row.get("direction", "")),
                    mfe=metric["post_mfe_r"],
                    mae=metric["post_mae_r"],
                    final=metric["final_delta_r"],
                    verdict=metric["verdict"],
                    reason=str(row.get("reason") or row.get("exit_reason", ""))[:48],
                )
            )


if __name__ == "__main__":
    main()
