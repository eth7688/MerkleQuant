"""Immutable public alerts and independent WeCom delivery for compression breakouts."""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from momentum_reflow_alerts import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_MS,
    WEBHOOK_PREFIX,
    load_alert_settings,
    send_wechat_markdown,
)
from momentum_compression_store import load_compression_state


_EVENT_LOCK_NAME = ".momentum_compression_alerts.lock"
_DELIVERY_LOCK_NAME = ".momentum_compression_alert_delivery.lock"
_EVENT_THREAD_LOCK = threading.RLock()
_DELIVERY_THREAD_LOCK = threading.Lock()
_STATE_VERSION = 1
_DEFAULT_STATE = {"version": _STATE_VERSION, "delivery_queue": []}


def _acquire_file_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0"); handle.flush(); os.fsync(handle.fileno())
        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _lock(path: Path, name: str, thread_lock):
    parent = Path(path).parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with thread_lock:
        with open(parent / name, "a+b") as handle:
            _acquire_file_lock(handle)
            try:
                yield
            finally:
                _release_file_lock(handle)


@contextmanager
def _event_lock(events_path: Path):
    with _lock(events_path, _EVENT_LOCK_NAME, _EVENT_THREAD_LOCK):
        yield


@contextmanager
def _delivery_lock(state_path: Path):
    with _lock(state_path, _DELIVERY_LOCK_NAME, _DELIVERY_THREAD_LOCK):
        yield


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _is_int(value: object) -> bool:
    return type(value) is int


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _validate_event(event: dict) -> None:
    if not isinstance(event, dict):
        raise ValueError("compression alert event is invalid")
    required = {"alert_id", "compression_id", "state", "event_at", "live_price", "htf_alignment", "structure", "created_at"}
    if set(event) != required:
        raise ValueError("compression alert event is invalid")
    if (not _is_int(event["alert_id"]) or event["alert_id"] <= 0
            or not isinstance(event["compression_id"], str) or not event["compression_id"]
            or event["state"] not in {"BREAKOUT_FRESH_LONG", "BREAKOUT_FRESH_SHORT"}
            or not _is_int(event["event_at"]) or event["event_at"] < 0
            or not _is_int(event["created_at"]) or event["created_at"] < 0
            or not _finite(event["live_price"])
            or event["htf_alignment"] not in {"CONFIRMED", "CONFLICT", "UNKNOWN"}
            or not isinstance(event["structure"], dict)):
        raise ValueError("compression alert event is invalid")
    structure = event["structure"]
    if (not isinstance(structure.get("symbol"), str) or not structure["symbol"]
            or structure.get("side") not in {"LONG", "SHORT"}):
        raise ValueError("compression alert event is invalid")


def _read_events_unlocked(events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    events = []
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                _validate_event(event)
                events.append(event)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("compression alert events are unreadable") from error
    ids = [event["alert_id"] for event in events]
    keys = [event["compression_id"] for event in events]
    if ids != list(range(1, len(events) + 1)) or len(set(keys)) != len(keys):
        raise ValueError("compression alert events sequence is invalid")
    return events


def _load_state_unlocked(state_path: Path) -> dict:
    if not state_path.exists():
        return copy.deepcopy(_DEFAULT_STATE)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("compression alert delivery state is unreadable") from error
    if not isinstance(state, dict) or set(state) != set(_DEFAULT_STATE) or state.get("version") != _STATE_VERSION:
        raise ValueError("compression alert delivery state is invalid")
    queue = state["delivery_queue"]
    if not isinstance(queue, list):
        raise ValueError("compression alert delivery state is invalid")
    ids = set()
    for item in queue:
        event = item.get("event") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or set(item) != {"alert_id", "event", "status", "attempts", "last_attempt_at", "next_attempt_at", "last_error"}
                or not _is_int(item.get("alert_id")) or item["alert_id"] <= 0
                or item["status"] not in {"pending", "in_flight", "delivered", "failed", "indeterminate"}
                or not _is_int(item["attempts"]) or not 0 <= item["attempts"] <= MAX_ATTEMPTS
                or not _is_int(item["last_attempt_at"]) or item["last_attempt_at"] < 0
                or not _is_int(item["next_attempt_at"]) or item["next_attempt_at"] < 0
                or not isinstance(item["last_error"], str) or not isinstance(event, dict)):
            raise ValueError("compression alert delivery state is invalid")
        _validate_event(event)
        if item["alert_id"] != event["alert_id"] or event["htf_alignment"] != "CONFIRMED" or item["alert_id"] in ids:
            raise ValueError("compression alert delivery state is invalid")
        ids.add(item["alert_id"])
    return state


def _public_event(source: dict, alert_id: int, now_ms: int) -> dict:
    event = {
        "alert_id": alert_id, "compression_id": source.get("compression_id"),
        "state": source.get("state"), "event_at": source.get("event_at"),
        "live_price": source.get("live_price"), "htf_alignment": source.get("htf_alignment"),
        "structure": copy.deepcopy(source.get("structure")), "created_at": now_ms,
    }
    _validate_event(event)
    return event


def _recover_confirmed_queue_unlocked(state: dict, events: list[dict]) -> bool:
    """Restore queue rows lost after a durable JSONL append but before state write."""
    queued_ids = {item["alert_id"] for item in state["delivery_queue"]}
    recovered = False
    for event in events:
        if event["htf_alignment"] != "CONFIRMED" or event["alert_id"] in queued_ids:
            continue
        state["delivery_queue"].append({
            "alert_id": event["alert_id"], "event": copy.deepcopy(event),
            "status": "pending", "attempts": 0, "last_attempt_at": 0,
            "next_attempt_at": event["created_at"], "last_error": "",
        })
        queued_ids.add(event["alert_id"])
        recovered = True
    return recovered


def _mark_interrupted_deliveries_unlocked(state: dict) -> bool:
    """A WeCom send may have succeeded before a crash; never auto-send it again."""
    recovered = False
    for item in state["delivery_queue"]:
        if item["status"] == "in_flight":
            item["status"] = "indeterminate"
            item["last_error"] = "delivery outcome is indeterminate after interrupted HTTP attempt"
            item["next_attempt_at"] = 0
            recovered = True
    return recovered


def append_compression_alerts(events_path: Path, state_path: Path, events: list[dict], now_ms: int) -> list[dict]:
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise TypeError("compression alert events must be a list of objects")
    if not _is_int(now_ms) or now_ms < 0:
        raise ValueError("now_ms must be a nonnegative integer")
    events_path, state_path = Path(events_path), Path(state_path)
    if events_path.parent.resolve() != state_path.parent.resolve():
        raise ValueError("compression alert files must share a directory")
    with _event_lock(events_path):
        stored = _read_events_unlocked(events_path)
        state = _load_state_unlocked(state_path)
        recovered = _recover_confirmed_queue_unlocked(state, stored)
        known = {event["compression_id"] for event in stored}
        created = []
        for source in events:
            candidate = _public_event(source, len(stored) + len(created) + 1, now_ms)
            if candidate["compression_id"] in known:
                continue
            known.add(candidate["compression_id"])
            created.append(candidate)
            if candidate["htf_alignment"] == "CONFIRMED":
                state["delivery_queue"].append({
                    "alert_id": candidate["alert_id"], "event": copy.deepcopy(candidate),
                    "status": "pending", "attempts": 0, "last_attempt_at": 0,
                    "next_attempt_at": now_ms, "last_error": "",
                })
        if created:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in created:
                    handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush(); os.fsync(handle.fileno())
            _atomic_write(state_path, state)
        elif recovered:
            _atomic_write(state_path, state)
        return copy.deepcopy(created)


def drain_compression_outbox(compression_state_path: Path, events_path: Path, alert_state_path: Path, now_ms: int) -> list[dict]:
    """Idempotently publish every durable FRESH event after a restart or callback failure."""
    state = load_compression_state(Path(compression_state_path))
    return append_compression_alerts(events_path, alert_state_path, state["fresh_outbox"], now_ms)


def read_public_compression_alerts(events_path: Path, after_id: int) -> dict:
    if not _is_int(after_id) or after_id < 0:
        raise ValueError("after_id must be a nonnegative integer")
    with _event_lock(Path(events_path)):
        events = _read_events_unlocked(Path(events_path))
        return {"latest_alert_id": len(events), "events": copy.deepcopy([event for event in events if event["alert_id"] > after_id][-20:])}


def compression_sound_available_ids(events_path: Path) -> set[str]:
    """Return all durable browser-alert identities without the public polling cap."""
    with _event_lock(Path(events_path)):
        return {event["compression_id"] for event in _read_events_unlocked(Path(events_path))}


def baseline_compression_alerts(events_path: Path) -> int:
    with _event_lock(Path(events_path)):
        return len(_read_events_unlocked(Path(events_path)))


def compression_delivery_statuses(state_path: Path) -> dict[str, str]:
    """Return the durable WeChat queue state by immutable compression identity."""
    with _delivery_lock(Path(state_path)):
        state = _load_state_unlocked(Path(state_path))
        return {
            item["event"]["compression_id"]: ("indeterminate" if item["status"] == "in_flight" else item["status"])
            for item in state["delivery_queue"]
        }


def _safe_error(error: object, webhook: str) -> str:
    text = str(error).replace(webhook, "<redacted webhook>")
    if WEBHOOK_PREFIX in text:
        text = text.split(WEBHOOK_PREFIX, 1)[0] + "<redacted webhook>"
    return text[:120]


def format_compression_wechat_markdown(event: dict) -> str:
    structure = event["structure"]
    observed = datetime.fromtimestamp(event["event_at"] / 1000, ZoneInfo("Asia/Shanghai"))
    return "\n".join((
        "【AXIOM 动能压缩破位警报】", "",
        f"{structure['symbol']} · {structure['side']}",
        f"触发价格：{event['live_price']}", f"状态：{event['state']}",
        f"突破边界/缓冲：{structure.get('upper_boundary_price', '--') if structure['side'] == 'LONG' else structure.get('lower_boundary_price', '--')} / {structure.get('breakout_buffer_price', '--')}",
        f"上沿：{structure.get('upper_boundary_price', '--')}",
        f"下沿：{structure.get('lower_boundary_price', '--')}",
        f"ATR14：{structure.get('atr14', '--')} · 质量：{structure.get('quality_score', '--')}",
        f"K线：{structure.get('compression_bars', '--')} · 方向触碰：{structure.get('directional_touch_count', '--')} · 收敛：{structure.get('contraction_ratio', '--')}",
        f"1H：{structure.get('htf_timeframes', {}).get('1h', '--')} · 4H：{structure.get('htf_timeframes', {}).get('4h', '--')} · 汇总：{event['htf_alignment']}",
        f"快照：{structure.get('ohlcv_snapshot_ref', '--')}",
        f"压缩标识：{event['compression_id']}",
        f"触发时间：{observed:%Y-%m-%d %H:%M} 北京时间",
    ))


def deliver_due_compression_wechat(settings_path: Path, state_path: Path, events_path: Path, now_ms: int, *, post=requests.post) -> dict:
    if not _is_int(now_ms):
        raise TypeError("now_ms must be an integer")
    if now_ms < 0:
        raise ValueError("now_ms must be nonnegative")
    settings_path, state_path, events_path = Path(settings_path), Path(state_path), Path(events_path)
    if len({path.parent.resolve() for path in (settings_path, state_path, events_path)}) != 1:
        raise ValueError("compression alert files must share a directory")
    with _delivery_lock(state_path):
        settings = load_alert_settings(settings_path)
        if not settings["wechat_enabled"]:
            return {"status": "disabled"}
        with _event_lock(events_path):
            state = _load_state_unlocked(state_path)
            if (_recover_confirmed_queue_unlocked(state, _read_events_unlocked(events_path))
                    or _mark_interrupted_deliveries_unlocked(state)):
                _atomic_write(state_path, state)
            item = next((item for item in state["delivery_queue"] if item["status"] == "pending"), None)
            if item is None:
                return {"status": "idle"}
            if item["next_attempt_at"] > now_ms:
                return {"status": "waiting_retry", "alert_id": item["alert_id"]}
            event = copy.deepcopy(item["event"])
            # WeCom has no idempotency key: persist intent before HTTP so a
            # post-send crash is reported as indeterminate, not duplicated.
            item["status"] = "in_flight"
            item["attempts"] += 1
            item["last_attempt_at"] = now_ms
            item["next_attempt_at"] = 0
            item["last_error"] = ""
            _atomic_write(state_path, state)
        try:
            send_wechat_markdown(settings["wechat_webhook"], format_compression_wechat_markdown(event), post=post)
            outcome, error_text = "delivered", ""
        except Exception as error:
            outcome, error_text = "retry_pending", _safe_error(error, settings["wechat_webhook"])
        with _event_lock(events_path):
            state = _load_state_unlocked(state_path)
            current = next(item for item in state["delivery_queue"] if item["alert_id"] == event["alert_id"])
            current["last_error"] = error_text
            if outcome == "delivered":
                current["status"] = "delivered"; current["next_attempt_at"] = 0
            elif current["attempts"] >= MAX_ATTEMPTS:
                current["status"] = "failed"; current["next_attempt_at"] = 0; outcome = "failed"
            else:
                current["status"] = "pending"
                current["next_attempt_at"] = now_ms + RETRY_DELAYS_MS[min(current["attempts"] - 1, len(RETRY_DELAYS_MS) - 1)]
            _atomic_write(state_path, state)
        return {"status": outcome, "alert_id": event["alert_id"], "error": error_text}
