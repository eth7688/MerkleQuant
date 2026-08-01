from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from numbers import Real
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


SETTINGS_VERSION = 1
LEDGER_VERSION = 1
WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
_LOCK = threading.RLock()
_DELIVERY_LOCK = threading.Lock()
_STATE_LOCK_NAME = ".momentum_reflow_alerts.lock"
_DELIVERY_LOCK_NAME = ".momentum_reflow_alert_delivery.lock"
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
RETRY_DELAYS_MS = (60_000, 300_000, 900_000, 3_600_000)
MAX_ATTEMPTS = 5
DAILY_LABELS = {
    "strong_momentum": "强动能日K",
    "bullish_engulfing": "看涨吞没",
    "bearish_engulfing": "看跌吞没",
    "hammer": "锤子线",
    "shooting_star": "流星线",
    "morning_star": "早晨之星",
    "evening_star": "黄昏之星",
    "bottom_fractal": "底分型",
    "top_fractal": "顶分型",
}


def _acquire_state_file_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_state_file_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _state_lock(*paths: Path):
    parents = {Path(path).parent.resolve() for path in paths}
    if len(parents) != 1:
        raise ValueError("reflow alert state files must share a directory")
    parent = parents.pop()
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / _STATE_LOCK_NAME
    with _LOCK:
        with open(lock_path, "a+b") as handle:
            _acquire_state_file_lock(handle)
            try:
                yield
            finally:
                _release_state_file_lock(handle)


@contextmanager
def _delivery_lock(*paths: Path):
    parents = {Path(path).parent.resolve() for path in paths}
    if len(parents) != 1:
        raise ValueError("reflow alert state files must share a directory")
    parent = parents.pop()
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / _DELIVERY_LOCK_NAME
    # Delivery serialization is outermost; general state locks remain short-lived.
    with _DELIVERY_LOCK:
        with open(lock_path, "a+b") as handle:
            _acquire_state_file_lock(handle)
            try:
                yield
            finally:
                _release_state_file_lock(handle)


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
    if value["wechat_webhook"]:
        validate_wechat_webhook(value["wechat_webhook"])
    elif value["wechat_enabled"]:
        raise ValueError("wechat webhook is required")
    return value


def _validate_ledger(value: dict) -> dict:
    if (value.get("version") != LEDGER_VERSION
            or type(value.get("initialized")) is not bool
            or type(value.get("next_alert_id")) is not int
            or type(value.get("wechat_cursor")) is not int
            or not isinstance(value.get("observed"), dict)
            or not isinstance(value.get("events"), list)):
        raise ValueError("reflow alert ledger version or shape is invalid")
    if (not value["initialized"]
            and (value["next_alert_id"] != 1 or value["wechat_cursor"] != 0
                 or value["observed"] or value["events"])):
        raise ValueError("reflow alert uninitialized ledger is invalid")
    for key, observed in value["observed"].items():
        if (not isinstance(key, str) or not key or not isinstance(observed, dict)
                or type(observed.get("ever_high")) is not bool
                or type(observed.get("last_quality_label")) is not str):
            raise ValueError("reflow alert observed entry is invalid")
    signal_keys = set()
    for expected_alert_id, event in enumerate(value["events"], start=1):
        delivery = event.get("wechat") if isinstance(event, dict) else None
        if (not isinstance(event, dict)
                or type(event.get("alert_id")) is not int
                or event["alert_id"] != expected_alert_id
                or type(event.get("signal_key")) is not str
                or not event["signal_key"]
                or event.get("trigger") not in {"new_signal", "upgraded_high"}
                or type(event.get("created_at")) is not int
                or event["created_at"] < 0
                or not isinstance(event.get("snapshot"), dict)
                or not isinstance(delivery, dict)
                or delivery.get("status") not in {"pending", "delivered", "failed"}
                or type(delivery.get("attempts")) is not int
                or not 0 <= delivery["attempts"] <= MAX_ATTEMPTS
                or type(delivery.get("last_attempt_at")) is not int
                or delivery["last_attempt_at"] < 0
                or type(delivery.get("next_attempt_at")) is not int
                or delivery["next_attempt_at"] < 0
                or type(delivery.get("last_error")) is not str):
            raise ValueError("reflow alert event entry is invalid")
        if event["signal_key"] in signal_keys:
            raise ValueError("reflow alert event signal key is duplicated")
        signal_keys.add(event["signal_key"])
        observed = value["observed"].get(event["signal_key"])
        if not observed or not observed["ever_high"]:
            raise ValueError("reflow alert event observation is invalid")
        if set(event["snapshot"]) != set(SNAPSHOT_FIELDS):
            raise ValueError("reflow alert event snapshot is invalid")
        _validate_snapshot(event["snapshot"])
        status = delivery["status"]
        if event["alert_id"] > value["wechat_cursor"] and status != "pending":
            raise ValueError("reflow alert terminal event is above delivery cursor")
        if status == "pending":
            if (delivery["attempts"] >= MAX_ATTEMPTS
                    or delivery["next_attempt_at"] < event["created_at"]
                    or delivery["attempts"] == 0
                    and (delivery["last_attempt_at"] != 0 or delivery["last_error"])
                    or delivery["attempts"] > 0
                    and delivery["next_attempt_at"] <= delivery["last_attempt_at"]):
                raise ValueError("reflow alert pending delivery state is invalid")
        elif status == "delivered":
            if (delivery["attempts"] == 0 or delivery["next_attempt_at"] != 0
                    or delivery["last_error"]):
                raise ValueError("reflow alert delivered state is invalid")
        elif delivery["attempts"] != MAX_ATTEMPTS or delivery["next_attempt_at"] != 0:
            raise ValueError("reflow alert failed state is invalid")
    last_alert_id = len(value["events"])
    if (value["next_alert_id"] != last_alert_id + 1
            or not 0 <= value["wechat_cursor"] <= last_alert_id):
        raise ValueError("reflow alert ledger sequence is invalid")
    return value


def _validate_snapshot(snapshot: dict) -> None:
    if (not isinstance(snapshot["symbol"], str) or not snapshot["symbol"]
            or not isinstance(snapshot["direction"], str) or not snapshot["direction"]
            or not isinstance(snapshot["daily_kind"], str) or not snapshot["daily_kind"]
            or type(snapshot["window_index"]) is not int or snapshot["window_index"] <= 0
            or type(snapshot["first_seen_at"]) is not int or snapshot["first_seen_at"] < 0):
        raise ValueError("reflow alert event snapshot is invalid")
    for field in ("price", "ema50"):
        if (isinstance(snapshot[field], bool) or not isinstance(snapshot[field], Real)
                or not math.isfinite(snapshot[field])):
            raise ValueError("reflow alert event snapshot is invalid")
    ratio = snapshot["breakout_volume_ratio"]
    if (isinstance(ratio, bool) or not isinstance(ratio, Real)
            or not math.isfinite(ratio) or ratio < 0):
        raise ValueError("reflow alert event snapshot is invalid")


def _load_alert_settings_unlocked(path: Path) -> dict:
    if not path.exists():
        _atomic_write(path, DEFAULT_SETTINGS.copy())
    return copy.deepcopy(_validate_settings(_read_object(path, "reflow alert settings")))


def load_alert_settings(path: Path) -> dict:
    with _state_lock(path):
        return _load_alert_settings_unlocked(path)


def validate_wechat_webhook(webhook: str) -> None:
    if not webhook.startswith(WEBHOOK_PREFIX) or not webhook[len(WEBHOOK_PREFIX):]:
        raise ValueError("invalid enterprise wechat webhook")


def save_alert_settings(path: Path, *, wechat_enabled: bool, wechat_webhook: str | None,
                        updated_by: str, now_ms: int) -> dict:
    if (type(wechat_enabled) is not bool or type(now_ms) is not int
            or not isinstance(updated_by, str)):
        raise TypeError("invalid reflow alert settings input")
    with _state_lock(path):
        current = _load_alert_settings_unlocked(path)
        supplied = "" if wechat_webhook is None else str(wechat_webhook).strip()
        if supplied:
            validate_wechat_webhook(supplied)
            current["wechat_webhook"] = supplied
        if wechat_enabled and not current["wechat_webhook"]:
            raise ValueError("wechat webhook is required")
        current.update(wechat_enabled=wechat_enabled, updated_at=now_ms, updated_by=updated_by)
        _atomic_write(path, current)
        return copy.deepcopy(current)


def enable_wechat_alerts(settings_path: Path, ledger_path: Path, *,
                         wechat_webhook: str | None, updated_by: str,
                         now_ms: int) -> dict:
    if (type(now_ms) is not int or not isinstance(updated_by, str)
            or wechat_webhook is not None and not isinstance(wechat_webhook, str)):
        raise TypeError("invalid reflow alert settings input")
    with _state_lock(settings_path, ledger_path):
        current = _load_alert_settings_unlocked(settings_path)
        supplied = "" if wechat_webhook is None else wechat_webhook.strip()
        candidate = supplied or current["wechat_webhook"]
        validate_wechat_webhook(candidate)
        if supplied:
            current["wechat_webhook"] = supplied
        if not current["wechat_enabled"]:
            ledger = _load_ledger(ledger_path)
            ledger["wechat_cursor"] = ledger["next_alert_id"] - 1
            _atomic_write(ledger_path, ledger)
        current.update(wechat_enabled=True, updated_at=now_ms, updated_by=updated_by)
        _atomic_write(settings_path, current)
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
    if now_ms < 0:
        raise ValueError("reflow alert observation timestamp must be nonnegative")
    with _state_lock(path):
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
    with _state_lock(path):
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
    with _state_lock(path):
        ledger = _load_ledger(path)
        event = next((item for item in reversed(ledger["events"])
                      if item["wechat"]["last_attempt_at"] > 0), None)
        if event is None:
            return {"last_delivery_at": 0, "last_delivery_status": "none",
                    "last_delivery_alert_id": 0, "last_delivery_error": ""}
        delivery = event["wechat"]
        return {"last_delivery_at": delivery["last_attempt_at"],
                "last_delivery_status": delivery["status"],
                "last_delivery_alert_id": event["alert_id"],
                "last_delivery_error": delivery["last_error"]}


def baseline_wechat_delivery(path: Path) -> int:
    with _state_lock(path):
        ledger = _load_ledger(path)
        ledger["wechat_cursor"] = ledger["next_alert_id"] - 1
        _atomic_write(path, ledger)
        return ledger["wechat_cursor"]


def _safe_error(error: object) -> str:
    text = str(error)
    if WEBHOOK_PREFIX in text:
        text = text.split(WEBHOOK_PREFIX, 1)[0] + WEBHOOK_PREFIX + "****"
    return text[:120]


def format_wechat_markdown(event: dict) -> str:
    item = event["snapshot"]
    first_seen = datetime.fromtimestamp(item["first_seen_at"] / 1000, ZoneInfo("Asia/Shanghai"))
    trigger = ("标准信号升级为高质量" if event["trigger"] == "upgraded_high"
               else "新高质量信号")
    return "\n".join((
        "【AXIOM 高质量回流警报】", "",
        f"{item['symbol']} · {item['direction']}",
        f"触发：{trigger}", f"价格：{item['price']}", f"EMA50：{item['ema50']}",
        f"日线：{DAILY_LABELS.get(item['daily_kind'], item['daily_kind'])}",
        f"窗口：{item['window_index']}/5", f"量比：{item['breakout_volume_ratio']}x",
        f"首次发现：{first_seen:%Y-%m-%d %H:%M} 北京时间",
    ))


def send_wechat_markdown(webhook: str, content: str, *, post=requests.post,
                         timeout: float = 5.0) -> None:
    validate_wechat_webhook(webhook)
    response = post(webhook, json={"msgtype": "markdown", "markdown": {"content": content}},
                    timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("errcode") != 0:
        errcode = payload.get("errcode", "invalid") if isinstance(payload, dict) else "invalid"
        raise RuntimeError(f"enterprise wechat rejected request: {errcode}")


def deliver_due_wechat(settings_path: Path, ledger_path: Path, now_ms: int, *,
                       post=requests.post) -> dict:
    if type(now_ms) is not int:
        raise TypeError("now_ms must be an integer")
    if now_ms < 0:
        raise ValueError("now_ms must be nonnegative")
    with _delivery_lock(settings_path, ledger_path):
        return _deliver_due_wechat(settings_path, ledger_path, now_ms, post=post)


def _deliver_due_wechat(settings_path: Path, ledger_path: Path, now_ms: int, *,
                         post=requests.post) -> dict:
    settings = load_alert_settings(settings_path)
    if not settings["wechat_enabled"]:
        return {"status": "disabled"}
    with _state_lock(ledger_path):
        ledger = _load_ledger(ledger_path)
        event = next((item for item in ledger["events"]
                      if item["alert_id"] > ledger["wechat_cursor"]), None)
    if event is None:
        return {"status": "idle"}
    if event["wechat"]["status"] != "pending":
        raise ValueError("reflow alert selected delivery is not pending")
    if event["wechat"]["next_attempt_at"] > now_ms:
        return {"status": "waiting_retry", "alert_id": event["alert_id"]}
    try:
        send_wechat_markdown(settings["wechat_webhook"], format_wechat_markdown(event), post=post)
        outcome, error_text = "delivered", ""
    except Exception as error:
        outcome, error_text = "retry_pending", _safe_error(error)
    with _state_lock(ledger_path):
        ledger = _load_ledger(ledger_path)
        current = next(item for item in ledger["events"] if item["alert_id"] == event["alert_id"])
        delivery = current["wechat"]
        delivery["attempts"] += 1
        delivery["last_attempt_at"] = now_ms
        delivery["last_error"] = error_text
        if outcome == "delivered":
            delivery["status"] = "delivered"
            delivery["next_attempt_at"] = 0
            ledger["wechat_cursor"] = max(
                ledger["wechat_cursor"], current["alert_id"]
            )
        elif delivery["attempts"] >= MAX_ATTEMPTS:
            delivery["status"] = "failed"
            delivery["next_attempt_at"] = 0
            ledger["wechat_cursor"] = max(
                ledger["wechat_cursor"], current["alert_id"]
            )
            outcome = "failed"
        else:
            delivery["status"] = "pending"
            delay = RETRY_DELAYS_MS[min(delivery["attempts"] - 1, len(RETRY_DELAYS_MS) - 1)]
            delivery["next_attempt_at"] = now_ms + delay
        _atomic_write(ledger_path, ledger)
    return {"status": outcome, "alert_id": event["alert_id"], "error": error_text}


def test_wechat_webhook(settings_path: Path, now_ms: int, *, post=requests.post) -> dict:
    if type(now_ms) is not int:
        raise TypeError("now_ms must be an integer")
    settings = load_alert_settings(settings_path)
    try:
        send_wechat_markdown(settings["wechat_webhook"], "【AXIOM】企业微信警报测试成功", post=post)
        settings.update(last_test_at=now_ms, last_test_ok=True, last_test_error="")
    except Exception as error:
        settings.update(last_test_at=now_ms, last_test_ok=False, last_test_error=_safe_error(error))
    with _state_lock(settings_path):
        current = _load_alert_settings_unlocked(settings_path)
        current.update(
            last_test_at=settings["last_test_at"],
            last_test_ok=settings["last_test_ok"],
            last_test_error=settings["last_test_error"],
        )
        _atomic_write(settings_path, current)
    return public_alert_settings(current)
