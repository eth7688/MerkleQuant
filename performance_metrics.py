import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


RANGE_DAYS = (7, 30, 90, 180, 365)
MAX_CUMULATIVE_R_POINTS = 500


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _record_r(record: dict) -> Optional[float]:
    risk = _finite_float(record.get("risk"))
    if risk is None or risk <= 0:
        return None
    persisted_value = record.get("r")
    if persisted_value is not None and str(persisted_value).strip():
        return _finite_float(persisted_value)
    pnl = _finite_float(record.get("pnl"))
    return None if pnl is None else pnl / risk


def build_r_trade_lifecycles(trade_log: list[dict]) -> dict:
    groups: OrderedDict[str, dict] = OrderedDict()
    valid_exit_record_count = 0
    excluded_records = 0

    for index, record in enumerate(trade_log or []):
        if (
            not isinstance(record, dict)
            or record.get("voided")
            or str(record.get("status") or "").strip().lower() == "voided"
        ):
            excluded_records += 1
            continue
        closed_at = _parse_time(record.get("time"))
        exit_r = _record_r(record)
        if closed_at is None or exit_r is None:
            excluded_records += 1
            continue

        valid_exit_record_count += 1
        signal_key = record.get("signal_key")
        group_key = signal_key if signal_key is not None and signal_key != "" else f"legacy:{index}"
        reason = str(record.get("reason") or "未标注").strip() or "未标注"
        mfe_r = _finite_float(record.get("mfe_r"))
        mae_r = _finite_float(record.get("mae_r"))

        trade = groups.setdefault(group_key, {
            "key": group_key,
            "direction": str(record.get("direction") or "").upper(),
            "time": closed_at.isoformat(),
            "timestamp": closed_at.timestamp(),
            "r": 0.0,
            "mfe_r": None,
            "mae_r": None,
            "reasons": [],
            "final_reason": reason,
            "reason_r": {},
        })
        trade["r"] += exit_r
        if mfe_r is not None:
            trade["mfe_r"] = (
                mfe_r if trade["mfe_r"] is None
                else max(trade["mfe_r"], mfe_r)
            )
        if mae_r is not None:
            trade["mae_r"] = (
                mae_r if trade["mae_r"] is None
                else max(trade["mae_r"], mae_r)
            )
        trade["reason_r"][reason] = trade["reason_r"].get(reason, 0.0) + exit_r
        if reason not in trade["reasons"]:
            trade["reasons"].append(reason)
        if closed_at.timestamp() >= trade["timestamp"]:
            trade["time"] = closed_at.isoformat()
            trade["timestamp"] = closed_at.timestamp()
            trade["final_reason"] = reason
            if record.get("direction"):
                trade["direction"] = str(record["direction"]).upper()

    lifecycles = sorted(groups.values(), key=lambda item: item["timestamp"])
    for trade in lifecycles:
        trade["r"] = round(trade["r"], 6)
        if trade["mfe_r"] is not None:
            trade["mfe_r"] = round(trade["mfe_r"], 6)
        if trade["mae_r"] is not None:
            trade["mae_r"] = round(trade["mae_r"], 6)
        trade["reason_r"] = {
            key: round(value, 6) for key, value in trade["reason_r"].items()
        }

    return {
        "source_record_count": len(trade_log or []),
        "valid_exit_record_count": valid_exit_record_count,
        "excluded_records": excluded_records,
        "lifecycles": lifecycles,
    }


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _cap_cumulative_points(points: list[dict]) -> list[dict]:
    if len(points) <= MAX_CUMULATIVE_R_POINTS:
        return points
    last_index = len(points) - 1
    denominator = MAX_CUMULATIVE_R_POINTS - 1
    return [
        points[index * last_index // denominator]
        for index in range(MAX_CUMULATIVE_R_POINTS)
    ]


def _summarize_lifecycles(lifecycles: list[dict], metadata: dict) -> dict:
    values = [float(item["r"]) for item in lifecycles]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_win = gross_profit / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    current_losses = 0
    max_losses = 0
    points = []
    directions = {}
    reasons = {}

    for trade in lifecycles:
        trade_r = float(trade["r"])
        cumulative += trade_r
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        points.append({"time": trade["time"], "r": _rounded(cumulative)})

        if trade_r < 0:
            current_losses += 1
            max_losses = max(max_losses, current_losses)
        else:
            current_losses = 0

        direction = trade["direction"] or "UNKNOWN"
        direction_row = directions.setdefault(direction, {"trades": 0, "net_r": 0.0})
        direction_row["trades"] += 1
        direction_row["net_r"] += trade_r

        for reason, reason_r in trade["reason_r"].items():
            reason_row = reasons.setdefault(reason, {"trades": 0, "net_r": 0.0})
            reason_row["trades"] += 1
            reason_row["net_r"] += reason_r

    measured_mfe = [
        trade["mfe_r"] for trade in lifecycles
        if trade["mfe_r"] is not None
    ]
    positive_mfe = [
        trade for trade in lifecycles
        if trade["r"] > 0
        and trade["mfe_r"] is not None
        and trade["mfe_r"] > 0
    ]
    mfe_denominator = sum(trade["mfe_r"] for trade in positive_mfe)
    capture = (
        sum(trade["r"] for trade in positive_mfe) / mfe_denominator
        if mfe_denominator > 0 else None
    )

    for row in directions.values():
        row["net_r"] = _rounded(row["net_r"])
    for row in reasons.values():
        row["net_r"] = _rounded(row["net_r"])

    count = len(values)
    return {
        **metadata,
        "valid_trade_count": count,
        "net_r": _rounded(sum(values)),
        "expectancy_r": _rounded(sum(values) / count) if count else None,
        "win_rate": _rounded(len(wins) / count * 100) if count else None,
        "average_win_r": _rounded(average_win) if average_win is not None else None,
        "average_loss_r": _rounded(average_loss) if average_loss is not None else None,
        "average_payoff_ratio": (
            _rounded(average_win / abs(average_loss))
            if average_win is not None and average_loss not in (None, 0) else None
        ),
        "profit_factor": _rounded(gross_profit / gross_loss) if gross_loss > 0 else None,
        "max_drawdown_r": _rounded(max_drawdown),
        "max_consecutive_losses": max_losses,
        "largest_win_r": _rounded(max(wins)) if wins else None,
        "largest_loss_r": _rounded(min(losses)) if losses else None,
        "direction_breakdown": directions,
        "exit_reason_breakdown": reasons,
        "average_mfe_r": (
            _rounded(sum(measured_mfe) / len(measured_mfe))
            if measured_mfe else None
        ),
        "mfe_capture_efficiency": _rounded(capture) if capture is not None else None,
        "cumulative_r_points": _cap_cumulative_points(points),
    }


def summarize_r_performance_ranges(
    trade_log: list[dict],
    now: Optional[datetime] = None,
) -> dict:
    grouped = build_r_trade_lifecycles(trade_log)
    current = now or (datetime.now(timezone.utc) + timedelta(hours=8))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    metadata = {
        "source_record_count": grouped["source_record_count"],
        "valid_exit_record_count": grouped["valid_exit_record_count"],
        "excluded_records": grouped["excluded_records"],
    }
    ranges = {
        "all": _summarize_lifecycles(grouped["lifecycles"], metadata),
    }
    now_timestamp = current.timestamp()
    for days in RANGE_DAYS:
        cutoff = now_timestamp - days * 86400
        selected = [
            trade for trade in grouped["lifecycles"]
            if trade["timestamp"] >= cutoff
        ]
        ranges[str(days)] = _summarize_lifecycles(selected, metadata)

    return {
        "status": "ok" if grouped["lifecycles"] else "empty",
        "ranges": ranges,
    }
