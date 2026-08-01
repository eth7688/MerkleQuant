from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path


SETTINGS_VERSION = 1
LEDGER_VERSION = 1
WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
_LOCK = threading.RLock()
DEFAULT_SETTINGS = {
    "version": SETTINGS_VERSION,
    "wechat_enabled": False,
    "wechat_webhook": "",
    "updated_at": 0,
    "updated_by": "",
    "last_test_at": 0,
    "last_test_ok": False,
    "last_test_error": "",
}
DEFAULT_LEDGER = {
    "version": LEDGER_VERSION,
    "initialized": False,
    "next_alert_id": 1,
    "wechat_cursor": 0,
    "observed": {},
    "events": [],
}
SNAPSHOT_FIELDS = (
    "symbol", "direction", "price", "ema50", "daily_kind", "window_index",
    "breakout_volume_ratio", "first_seen_at",
)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def _read_object(path: Path, description: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} is unreadable") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} shape is invalid")
    return value


def _validate_settings(value: dict) -> dict:
    if (value.get("version") != SETTINGS_VERSION
            or type(value.get("wechat_enabled")) is not bool
            or type(value.get("wechat_webhook")) is not str
            or type(value.get("updated_at")) is not int
            or type(value.get("updated_by")) is not str
            or type(value.get("last_test_at")) is not int
            or type(value.get("last_test_ok")) is not bool
            or type(value.get("last_test_error")) is not str):
        raise ValueError("reflow alert settings version or shape is invalid")
    return value


def _validate_ledger(value: dict) -> dict:
    if (value.get("version") != LEDGER_VERSION
            or type(value.get("initialized")) is not bool
            or type(value.get("next_alert_id")) is not int
            or type(value.get("wechat_cursor")) is not int
            or not isinstance(value.get("observed"), dict)
            or not isinstance(value.get("events"), list)):
        raise ValueError("reflow alert ledger version or shape is invalid")
    for key, observed in value["observed"].items():
        if (not isinstance(key, str) or not isinstance(observed, dict)
                or type(observed.get("ever_high")) is not bool
                or type(observed.get("last_quality_label")) is not str):
            raise ValueError("reflow alert observed entry is invalid")
    for event in value["events"]:
        delivery = event.get("wechat") if isinstance(event, dict) else None
        if (not isinstance(event, dict)
                or type(event.get("alert_id")) is not int
                or type(event.get("signal_key")) is not str
                or event.get("trigger") not in {"new_signal", "upgraded_high"}
                or type(event.get("created_at")) is not int
                or not isinstance(event.get("snapshot"), dict)
                or not isinstance(delivery, dict)
                or delivery.get("status") not in {"pending", "delivered", "failed"}
                or type(delivery.get("attempts")) is not int
                or type(delivery.get("last_attempt_at")) is not int
                or type(delivery.get("next_attempt_at")) is not int
                or type(delivery.get("last_error")) is not str):
            raise ValueError("reflow alert event entry is invalid")
    return value


def load_alert_settings(path: Path) -> dict:
    with _LOCK:
        if not path.exists():
            _atomic_write(path, DEFAULT_SETTINGS.copy())
        return copy.deepcopy(_validate_settings(_read_object(path, "reflow alert settings")))


def validate_wechat_webhook(webhook: str) -> None:
    if not webhook.startswith(WEBHOOK_PREFIX) or not webhook[len(WEBHOOK_PREFIX):]:
        raise ValueError("invalid enterprise wechat webhook")


def save_alert_settings(path: Path, *, wechat_enabled: bool, wechat_webhook: str | None,
                        updated_by: str, now_ms: int) -> dict:
    if (type(wechat_enabled) is not bool or type(now_ms) is not int
            or not isinstance(updated_by, str)):
        raise TypeError("invalid reflow alert settings input")
    with _LOCK:
        current = load_alert_settings(path)
        supplied = "" if wechat_webhook is None else str(wechat_webhook).strip()
        if supplied:
            validate_wechat_webhook(supplied)
            current["wechat_webhook"] = supplied
        if wechat_enabled and not current["wechat_webhook"]:
            raise ValueError("wechat webhook is required")
        current.update(wechat_enabled=wechat_enabled, updated_at=now_ms, updated_by=updated_by)
        _atomic_write(path, current)
        return copy.deepcopy(current)


def public_alert_settings(settings: dict) -> dict:
    public = {key: value for key, value in settings.items() if key != "wechat_webhook"}
    webhook = str(settings.get("wechat_webhook", ""))
    public["webhook_configured"] = bool(webhook)
    public["webhook_mask"] = f"****{webhook[-4:]}" if webhook else ""
    return public


def _load_ledger(path: Path) -> dict:
    if not path.exists():
        return copy.deepcopy(DEFAULT_LEDGER)
    return _validate_ledger(_read_object(path, "reflow alert ledger"))


def _is_high_active(row: dict) -> bool:
    return row.get("quality_label") == "HIGH" and row.get("status") == "ACTIVE"


def _snapshot(row: dict) -> dict:
    return {field: copy.deepcopy(row.get(field)) for field in SNAPSHOT_FIELDS}


def observe_reflow_alerts(path: Path, rows: list[dict], now_ms: int) -> list[dict]:
    if not isinstance(rows, list) or type(now_ms) is not int:
        raise TypeError("invalid reflow alert observation")
    with _LOCK:
        ledger = _load_ledger(path)
        created = []
        initializing = not ledger["initialized"]
        for row in rows:
            key = str(row.get("signal_key", ""))
            if not key:
                raise ValueError("signal_key is required")
            previously_seen = key in ledger["observed"]
            observed = ledger["observed"].setdefault(
                key, {"ever_high": False, "last_quality_label": ""},
            )
            qualifies = _is_high_active(row)
            if qualifies and not observed["ever_high"] and not initializing:
                alert_id = ledger["next_alert_id"]
                ledger["next_alert_id"] += 1
                event = {
                    "alert_id": alert_id, "signal_key": key, "created_at": now_ms,
                    "trigger": ("upgraded_high" if previously_seen
                                and observed["last_quality_label"] != "HIGH" else "new_signal"),
                    "snapshot": _snapshot(row),
                    "wechat": {"status": "pending", "attempts": 0,
                               "last_attempt_at": 0, "next_attempt_at": now_ms,
                               "last_error": ""},
                }
                ledger["events"].append(event)
                created.append(copy.deepcopy(event))
            if qualifies:
                observed["ever_high"] = True
            observed["last_quality_label"] = str(row.get("quality_label", ""))
        ledger["initialized"] = True
        _atomic_write(path, ledger)
        return created


def read_public_alerts(path: Path, after_id: int) -> dict:
    if type(after_id) is not int or after_id < 0:
        raise ValueError("after_id must be a nonnegative integer")
    with _LOCK:
        ledger = _load_ledger(path)
        latest = ledger["next_alert_id"] - 1
        events = []
        for event in ledger["events"]:
            if event["alert_id"] > after_id:
                events.append({"alert_id": event["alert_id"], "signal_key": event["signal_key"],
                               "trigger": event["trigger"], "created_at": event["created_at"],
                               **copy.deepcopy(event["snapshot"])})
        return {"latest_alert_id": latest, "events": events[-20:]}


def read_delivery_status(path: Path) -> dict:
    with _LOCK:
        ledger = _load_ledger(path)
        if not ledger["events"]:
            return {"last_delivery_at": 0, "last_delivery_status": "none",
                    "last_delivery_alert_id": 0, "last_delivery_error": ""}
        event = ledger["events"][-1]
        delivery = event["wechat"]
        return {"last_delivery_at": delivery["last_attempt_at"],
                "last_delivery_status": delivery["status"],
                "last_delivery_alert_id": event["alert_id"],
                "last_delivery_error": delivery["last_error"]}


def baseline_wechat_delivery(path: Path) -> int:
    with _LOCK:
        ledger = _load_ledger(path)
        ledger["wechat_cursor"] = ledger["next_alert_id"] - 1
        _atomic_write(path, ledger)
        return ledger["wechat_cursor"]
