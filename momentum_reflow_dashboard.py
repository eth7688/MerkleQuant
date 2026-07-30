from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SETTINGS_VERSION = 1
HISTORY_VERSION = 1
DEFAULT_REFLOW_SETTINGS = {
    "version": SETTINGS_VERSION,
    "auto_scan_enabled": True,
    "updated_at": 0,
    "updated_by": "",
}
DAILY_POINTS = {3: 30.0, 2: 24.0, 1: 18.0}
WINDOW_POINTS = {1: 10.0, 2: 8.0, 3: 6.0, 4: 4.0, 5: 2.0}
HOUR_MS = 3_600_000
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_settings(path: Path) -> dict:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("momentum reflow settings are unreadable") from error
    if (
        not isinstance(settings, dict)
        or type(settings.get("version")) is not int
        or settings.get("version") != SETTINGS_VERSION
        or type(settings.get("auto_scan_enabled")) is not bool
        or type(settings.get("updated_at")) is not int
        or type(settings.get("updated_by")) is not str
    ):
        raise ValueError("momentum reflow settings version or shape is invalid")
    return settings


def load_reflow_settings(path: Path) -> dict:
    if not path.exists():
        settings = DEFAULT_REFLOW_SETTINGS.copy()
        _atomic_write_json(path, settings)
        return settings
    return _read_settings(path)


def save_reflow_settings(
    path: Path, enabled: bool, updated_by: str, now_ms: int
) -> dict:
    if type(enabled) is not bool:
        raise TypeError("enabled must be a boolean")
    if not isinstance(updated_by, str):
        raise TypeError("updated_by must be a string")
    if type(now_ms) is not int:
        raise TypeError("now_ms must be an integer")
    settings = {
        "version": SETTINGS_VERSION,
        "auto_scan_enabled": enabled,
        "updated_at": now_ms,
        "updated_by": updated_by,
    }
    _atomic_write_json(path, settings)
    return settings


def _linear(value, start, end, start_points, end_points):
    value = min(max(float(value), start), end)
    ratio = (value - start) / (end - start)
    return start_points + ratio * (end_points - start_points)


def _required_finite_float(candidate: dict, field: str) -> float:
    try:
        value = float(candidate[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{field} is required and must be numeric") from error
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


def _required_point(candidate: dict, field: str, points: dict[int, float]) -> float:
    value = _required_finite_float(candidate, field)
    if not value.is_integer() or int(value) not in points:
        raise ValueError(f"{field} is invalid")
    return points[int(value)]


def score_reflow_candidate(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    daily = _required_point(candidate, "daily_rank", DAILY_POINTS)
    volume_ratio = _required_finite_float(candidate, "breakout_volume_ratio")
    expansion_atr = _required_finite_float(candidate, "max_expansion_atr")
    close_distance_atr = _required_finite_float(candidate, "close_distance_atr")
    window = _required_point(candidate, "window_index", WINDOW_POINTS)
    volume = _linear(volume_ratio, 1.5, 3.0, 15.0, 25.0)
    expansion = _linear(expansion_atr, 1.5, 3.0, 12.0, 20.0)
    distance = 15.0 * (1.0 - min(abs(close_distance_atr), 0.35) / 0.35)
    total = int(round(daily + volume + expansion + distance + window))
    label = "HIGH" if total >= 75 else "STANDARD" if total >= 60 else "WATCH"
    return {
        **candidate,
        "quality_score": min(100, max(0, total)),
        "quality_label": label,
        "score_components": {
            "daily": round(daily, 4),
            "volume": round(volume, 4),
            "expansion": round(expansion, 4),
            "distance": round(distance, 4),
            "window": round(window, 4),
        },
    }


def beijing_day(now_ms: int) -> str:
    if type(now_ms) is not int:
        raise ValueError("now_ms must be an integer")
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(
        BEIJING_TZ
    ).date().isoformat()


def reflow_signal_key(row: dict) -> str:
    return "|".join([
        row["symbol"],
        row["direction"],
        str(int(row["breakout_time"])),
        str(int(row["return_open_time"])),
    ])


def _load_json_object(path: Path, description: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} is unreadable") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must be an object")
    return payload


def _read_history(path: Path) -> dict:
    if not path.exists():
        return {"version": HISTORY_VERSION, "days": {}}
    history = _load_json_object(path, "momentum reflow history")
    if (
        type(history.get("version")) is not int
        or history.get("version") != HISTORY_VERSION
        or not isinstance(history.get("days"), dict)
    ):
        raise ValueError("momentum reflow history version or shape is invalid")
    for day in history["days"].values():
        if not isinstance(day, dict) or not isinstance(day.get("signals"), dict):
            raise ValueError("momentum reflow history day shape is invalid")
        for key, signal in day["signals"].items():
            if not isinstance(key, str) or not isinstance(signal, dict):
                raise ValueError("momentum reflow history signal shape is invalid")
            try:
                _require_identity(signal)
                if key != reflow_signal_key(signal):
                    raise ValueError("momentum reflow history signal key is invalid")
                score_reflow_candidate(signal)
            except ValueError as error:
                raise ValueError("momentum reflow history signal is invalid") from error
    return history


def _read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "symbols": {}}
    ledger = _load_json_object(path, "momentum reflow ledger")
    if ledger.get("version") != 1 or not isinstance(ledger.get("symbols"), dict):
        raise ValueError("momentum reflow ledger version or shape is invalid")
    for symbol, state in ledger["symbols"].items():
        if (
            not isinstance(symbol, str)
            or not isinstance(state, dict)
            or ("event" in state and state["event"] is not None and not isinstance(state["event"], dict))
        ):
            raise ValueError("momentum reflow ledger symbol shape is invalid")
    return ledger


def _require_identity(row: dict) -> None:
    if not isinstance(row, dict):
        raise ValueError("signal must be an object")
    for field in ("symbol", "direction"):
        if not isinstance(row.get(field), str) or not row[field]:
            raise ValueError(f"{field} is required")
    for field in ("breakout_time", "return_open_time"):
        value = _required_finite_float(row, field)
        if not value.is_integer():
            raise ValueError(f"{field} must be an integer")


def _event_keys(ledger: dict) -> dict[str, dict]:
    events = {}
    for symbol, state in ledger["symbols"].items():
        if not isinstance(symbol, str) or not isinstance(state, dict):
            continue
        event = state.get("event")
        if not isinstance(event, dict):
            continue
        row = {
            "symbol": symbol,
            "direction": event.get("direction"),
            "breakout_time": event.get("breakout_open_time"),
            "return_open_time": event.get("return_open_time"),
        }
        try:
            _require_identity(row)
            events[reflow_signal_key(row)] = event
        except ValueError:
            continue
    return events


def _set_status_from_event(row: dict, event: dict | None) -> None:
    if event is None:
        row.setdefault("status", "ACTIVE")
        row.setdefault("status_reason", "")
        return
    state = event.get("state")
    reason = event.get("audit_reason", "")
    if state == "RETURN_WINDOW":
        row["status"] = "ACTIVE"
        row["status_reason"] = reason
    elif state == "CONSUMED" and reason == "return_window_complete":
        row["status"] = "WINDOW_COMPLETE"
        row["status_reason"] = reason
    elif state in {"CONSUMED", "INVALIDATED"}:
        row["status"] = "INVALID"
        row["status_reason"] = reason


def _sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            -int(row["return_open_time"]),
            -int(row["quality_score"]),
            -float(row["breakout_volume_ratio"]),
            row["symbol"],
        ),
    )


def _dashboard(history: dict, day: str) -> dict:
    signals = history["days"].get(day, {"signals": {}})["signals"]
    return {"day": day, "rows": _sort_rows(copy.deepcopy(list(signals.values())))}


def merge_reflow_signals(
    history_path: Path, ledger_path: Path, scan_result: dict, now_ms: int
) -> dict:
    if not isinstance(scan_result, dict) or not isinstance(scan_result.get("rows"), list):
        raise ValueError("scan result rows are invalid")
    day = beijing_day(now_ms)
    history = _read_history(history_path)
    ledger_events = _event_keys(_read_ledger(ledger_path))
    signals = history["days"].setdefault(day, {"signals": {}})["signals"]

    for candidate in scan_result["rows"]:
        _require_identity(candidate)
        key = reflow_signal_key(candidate)
        existing = signals.get(key)
        scored = score_reflow_candidate(candidate)
        row = {
            **scored,
            "signal_key": key,
            "return_close_time": int(scored["return_open_time"]) + HOUR_MS,
            "first_seen_at": existing.get("first_seen_at", now_ms) if existing else now_ms,
            "last_seen_at": now_ms,
        }
        _set_status_from_event(row, ledger_events.get(key))
        signals[key] = row

    for key, row in signals.items():
        _set_status_from_event(row, ledger_events.get(key))

    _atomic_write_json(history_path, history)
    return _dashboard(history, day)


def load_reflow_dashboard(history_path: Path, now_ms: int) -> dict:
    return _dashboard(_read_history(history_path), beijing_day(now_ms))


def next_reflow_scan_at(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    beijing_now = now.astimezone(BEIJING_TZ)
    target = beijing_now.replace(minute=3, second=0, microsecond=0)
    if beijing_now >= target:
        target = target.replace(hour=(target.hour + 1) % 24)
        if target.hour == 0:
            target += timedelta(days=1)
    return target
