from __future__ import annotations

import math
import statistics


def summarize_positions(events: list[dict]) -> dict:
    positions: dict[str, dict] = {}
    for event in events:
        position_id = str(event.get("position_id", ""))
        if not position_id:
            continue
        row = positions.setdefault(position_id, {
            "risk": 0.0, "pnl": 0.0, "fees": 0.0, "mfe": 0.0, "mae": 0.0,
            "daily_pattern": {},
        })
        if event.get("type") == "entry_fill":
            row["risk"] = float(event.get("risk_usdt", 0) or 0)
            entry_fee = float(event.get("fee", 0) or 0)
            row["fees"] += entry_fee
            row["pnl"] -= entry_fee
            row["daily_pattern"] = event.get("daily_pattern", {}) or {}
        elif event.get("type") == "exit_fill":
            row["pnl"] += float(event.get("net_pnl", 0) or 0)
            row["fees"] += float(event.get("fee", 0) or 0)
            row["mfe"] = max(row["mfe"], float(event.get("mfe_r", 0) or 0))
            row["mae"] = max(row["mae"], float(event.get("mae_r", 0) or 0))
    complete = [row for row in positions.values() if row["risk"] > 0]
    r_values = [row["pnl"] / row["risk"] for row in complete]
    wins = [value for value in r_values if value > 0]
    losses = [value for value in r_values if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in r_values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    daily_groups = {}
    for row in complete:
        pattern = row.get("daily_pattern", {}) or {}
        alignment = (
            str(pattern.get("alignment", "none") or "none")
            if pattern.get("recorded") else "unavailable"
        )
        values = daily_groups.setdefault(alignment, [])
        values.append(row["pnl"] / row["risk"])
    daily_breakdown = {
        alignment: {
            "trades": len(values),
            "win_rate": round(sum(value > 0 for value in values) * 100 / len(values), 4),
            "sum_r": round(sum(values), 6),
            "mean_r": round(statistics.mean(values), 6),
        }
        for alignment, values in sorted(daily_groups.items())
    }
    return {
        "trades": len(r_values),
        "wins": len(wins),
        "win_rate": round(len(wins) * 100 / len(r_values), 4) if r_values else 0.0,
        "sum_r": round(sum(r_values), 6),
        "mean_r": round(statistics.mean(r_values), 6) if r_values else 0.0,
        "median_r": round(statistics.median(r_values), 6) if r_values else 0.0,
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss else None,
        "max_drawdown_r": round(max_drawdown, 6),
        "reach_1r": sum(row["mfe"] >= 1 for row in complete),
        "reach_2r": sum(row["mfe"] >= 2 for row in complete),
        "reach_3r": sum(row["mfe"] >= 3 for row in complete),
        "mean_mfe_r": round(statistics.mean(row["mfe"] for row in complete), 6) if complete else 0.0,
        "mean_mae_r": round(statistics.mean(row["mae"] for row in complete), 6) if complete else 0.0,
        "total_fees": round(sum(row["fees"] for row in complete), 8),
        "daily_pattern_breakdown": daily_breakdown,
    }
