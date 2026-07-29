import math
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional


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
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "reasons": [],
            "final_reason": reason,
            "reason_r": {},
        })
        trade["r"] += exit_r
        trade["mfe_r"] = max(trade["mfe_r"], mfe_r or 0.0)
        trade["mae_r"] = max(trade["mae_r"], mae_r or 0.0)
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
        trade["mfe_r"] = round(trade["mfe_r"], 6)
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
