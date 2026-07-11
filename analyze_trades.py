"""
AXIOM Quant trade analytics.

Usage:
  python analyze_trades.py trades_<uid>.jsonl --out trade_report.md
  python analyze_trades.py trades_*.jsonl replay_trades.jsonl
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_records(paths):
    rows = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                rows.append(d)
    return rows


def _f(row, key, default=0.0):
    try:
        return float(row.get(key, default) or default)
    except Exception:
        return default


def summarize(rows, group_keys):
    buckets = defaultdict(list)
    for r in rows:
        key = tuple(str(r.get(k, "--") or "--") for k in group_keys)
        buckets[key].append(r)

    out = []
    for key, items in sorted(buckets.items()):
        r_vals = [_f(x, "r", _f(x, "pnl", 0) / max(_f(x, "risk", 0), 1e-9)) for x in items]
        pnl_vals = [_f(x, "pnl", 0) for x in items]
        wins = [x for x in r_vals if x > 0]
        losses = [x for x in r_vals if x <= 0]
        mfe = [_f(x, "mfe_r", 0) for x in items]
        mae = [_f(x, "mae_r", 0) for x in items]
        hold = [_f(x, "hold_minutes", 0) for x in items if _f(x, "hold_minutes", 0) > 0]
        out.append({
            "group": key,
            "n": len(items),
            "pnl": round(sum(pnl_vals), 2),
            "sum_r": round(sum(r_vals), 3),
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else 0,
            "win_rate": round(len(wins) / len(r_vals) * 100, 1) if r_vals else 0,
            "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0,
            "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0,
            "avg_mfe_r": round(sum(mfe) / len(mfe), 3) if mfe else 0,
            "avg_mae_r": round(sum(mae) / len(mae), 3) if mae else 0,
            "avg_hold_min": round(sum(hold) / len(hold), 1) if hold else 0,
        })
    return out


def mfe_mae_report(rows):
    out = []
    for keys in [("interval",), ("interval", "direction"), ("reason",)]:
        for row in summarize(rows, keys):
            row["keys"] = keys
            out.append(row)
    return out


def render_markdown(rows):
    lines = ["# AXIOM Trade Report", ""]
    lines.append(f"Total records: {len(rows)}")
    total = summarize(rows, tuple())[0] if rows else {}
    if total:
        lines.append(f"Total PnL: {total['pnl']} | Sum R: {total['sum_r']} | Avg R: {total['avg_r']} | Win: {total['win_rate']}%")
    lines.append("")

    for title, keys in [
        ("By Interval", ("interval",)),
        ("By Direction", ("direction",)),
        ("By Interval And Direction", ("interval", "direction")),
        ("By BTC Regime", ("btc_regime",)),
        ("By Interval And BTC Regime", ("interval", "btc_regime")),
        ("By Direction And BTC Regime", ("direction", "btc_regime")),
        ("By BTC 4H State", ("btc_4h_overall",)),
        ("By BTC 1D State", ("btc_1d_overall",)),
        ("By Exit Reason", ("reason",)),
        ("By Interval And Reason", ("interval", "reason")),
    ]:
        lines.append(f"## {title}")
        lines.append("| Group | N | PnL | SumR | AvgR | Win% | AvgWinR | AvgLossR | MFE | MAE | HoldMin |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in summarize(rows, keys):
            group = " / ".join(r["group"]) if r["group"] else "ALL"
            lines.append(
                f"| {group} | {r['n']} | {r['pnl']} | {r['sum_r']} | {r['avg_r']} | "
                f"{r['win_rate']} | {r['avg_win_r']} | {r['avg_loss_r']} | "
                f"{r['avg_mfe_r']} | {r['avg_mae_r']} | {r['avg_hold_min']} |"
            )
        lines.append("")

    lines.append("## MFE/MAE Notes")
    lines.append("- `avg_mfe_r`低但`avg_mae_r`高: 入场质量或过滤条件需要收紧。")
    lines.append("- `avg_mfe_r`高但`avg_r`低: 止盈/追踪吐回过多或保本逻辑太慢。")
    lines.append("- 超时退出组若`avg_mfe_r`明显高于0.6: 超时阈值可能过严。")
    lines.append("- 止损组若`avg_mae_r`长期远大于1: 实盘滑点/止损执行可能失真。")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = load_records(args.paths)
    md = render_markdown(rows)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"report -> {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
