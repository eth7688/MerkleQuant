"""Persistent watch-pool state for the 15 minute compression scanner."""

import copy
import json
import math
import os
import tempfile
from pathlib import Path

import pandas as pd


STATE_VERSION = 1
_POOL_STATES = {
    "PRE_BREAKOUT", "COMPRESSION_ACTIVE_LONG", "COMPRESSION_ACTIVE_SHORT",
    "BREAKOUT_UNCONFIRMED_LONG", "BREAKOUT_UNCONFIRMED_SHORT",
    "BREAKOUT_FRESH_LONG", "BREAKOUT_FRESH_SHORT",
    "BREAKOUT_ACTIVE_LONG", "BREAKOUT_ACTIVE_SHORT",
    "BREAKOUT_RETRACING_LONG", "BREAKOUT_RETRACING_SHORT",
    "BOUNDARY_EXIT_ADVERSE_LONG", "BOUNDARY_EXIT_ADVERSE_SHORT",
}
_ELIGIBLE_DISCOVERY_STATES = {
    "PRE_BREAKOUT", "COMPRESSION_ACTIVE_LONG", "COMPRESSION_ACTIVE_SHORT",
}


def default_state():
    return {
        "version": STATE_VERSION,
        "auto_enabled": False,
        "next_event_id": 1,
        "pool": {},
        "episodes": {},
        "delivery_queue": [],
        "last_structure_scan_at": 0,
        "last_closed_15m_close_time": 0,
        "last_error": "",
    }


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonnegative_int(value, name):
    if not _is_int(value) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _require_finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")


def _validate_item(compression_id, item, *, allow_failed=False):
    if not isinstance(compression_id, str) or not compression_id or not isinstance(item, dict):
        raise ValueError("invalid pool item")
    if not isinstance(item.get("symbol"), str) or not item["symbol"]:
        raise ValueError("invalid pool symbol")
    if item.get("compression_id") != compression_id or item.get("side") not in {"LONG", "SHORT"}:
        raise ValueError("invalid pool identity")
    allowed_states = {"BREAKOUT_FAILED"} if allow_failed else _POOL_STATES
    if item.get("state") not in allowed_states or not isinstance(item.get("fresh_emitted"), bool):
        raise ValueError("invalid pool state")
    if item["state"].endswith("_LONG") and item["side"] != "LONG":
        raise ValueError("pool state does not match side")
    if item["state"].endswith("_SHORT") and item["side"] != "SHORT":
        raise ValueError("pool state does not match side")
    for key in ("upper_boundary_price", "lower_boundary_price", "breakout_buffer_price"):
        _require_finite_number(item.get(key), key)
    if item["upper_boundary_price"] <= item["lower_boundary_price"] or item["breakout_buffer_price"] < 0:
        raise ValueError("invalid pool boundaries")
    for key in ("first_seen_at", "last_verified_at"):
        _require_nonnegative_int(item.get(key), key)
    if "last_price_at" in item:
        _require_nonnegative_int(item["last_price_at"], "last_price_at")
    if "live_price" in item:
        _require_finite_number(item["live_price"], "live_price")


def _validate_state(state):
    if not isinstance(state, dict) or set(state) != set(default_state()):
        raise ValueError("invalid compression state")
    if state["version"] != STATE_VERSION or not isinstance(state["auto_enabled"], bool):
        raise ValueError("invalid compression state version or auto flag")
    for key in ("next_event_id", "last_structure_scan_at", "last_closed_15m_close_time"):
        _require_nonnegative_int(state[key], key)
    if not isinstance(state["last_error"], str):
        raise ValueError("invalid last error")
    for key in ("pool", "episodes"):
        if not isinstance(state[key], dict):
            raise ValueError(f"invalid {key}")
        for compression_id, item in state[key].items():
            _validate_item(compression_id, item, allow_failed=key == "episodes")
    if not isinstance(state["delivery_queue"], list) or not all(isinstance(event, dict) for event in state["delivery_queue"]):
        raise ValueError("invalid delivery queue")


def load_compression_state(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return default_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("compression state is corrupt") from error
    _validate_state(state)
    return state


def save_compression_state(path: Path, state: dict) -> None:
    _validate_state(state)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _eligible(evaluation):
    return evaluation.get("state") in _ELIGIBLE_DISCOVERY_STATES


def _pool_item(evaluation, now_ms, previous=None):
    item = copy.deepcopy(evaluation)
    item["first_seen_at"] = previous.get("first_seen_at", now_ms) if previous else now_ms
    item["last_verified_at"] = now_ms
    item["fresh_emitted"] = previous.get("fresh_emitted", False) if previous else False
    item["state"] = previous.get("state", evaluation["state"]) if previous else evaluation["state"]
    return item


def reconcile_structure_scan(state: dict, evaluations: list[dict], now_ms: int) -> tuple[dict, dict]:
    _validate_state(state)
    _require_nonnegative_int(now_ms, "now_ms")
    if not isinstance(evaluations, list) or not all(isinstance(row, dict) for row in evaluations):
        raise ValueError("evaluations must be a list of dictionaries")
    out = copy.deepcopy(state)
    report = {"fresh_events": [], "added_compression_ids": [], "removed_compression_ids": []}
    for evaluation in evaluations:
        compression_id = evaluation.get("compression_id")
        if not isinstance(compression_id, str) or not compression_id:
            raise ValueError("evaluation compression_id is required")
        prior = out["pool"].get(compression_id)
        if _eligible(evaluation):
            if prior is None:
                report["added_compression_ids"].append(compression_id)
            item = _pool_item(evaluation, now_ms, prior)
            terminal = out["episodes"].get(compression_id)
            if terminal is not None and terminal["fresh_emitted"]:
                item["fresh_emitted"] = True
            out["pool"][compression_id] = item
            continue
        if compression_id in out["pool"]:
            del out["pool"][compression_id]
            report["removed_compression_ids"].append(compression_id)
    out["last_structure_scan_at"] = now_ms
    _validate_state(out)
    return out, report


def _event(state, item, event_state, price, now_ms):
    event = {
        "event_id": state["next_event_id"],
        "compression_id": item["compression_id"],
        "state": event_state,
        "event_at": now_ms,
        "live_price": price,
        "htf_alignment": item.get("htf_alignment", "UNKNOWN"),
        "structure": copy.deepcopy(item),
    }
    state["next_event_id"] += 1
    if event["htf_alignment"] == "CONFIRMED":
        state["delivery_queue"].append(copy.deepcopy(event))
    return event


def _live_transition(item, price):
    side = item["side"]
    upper, lower, buffer_price = (item["upper_boundary_price"], item["lower_boundary_price"], item["breakout_buffer_price"])
    fresh = price > upper + buffer_price if side == "LONG" else price < lower - buffer_price
    outward = price >= upper if side == "LONG" else price <= lower
    inside = lower <= price <= upper
    adverse = price < lower if side == "LONG" else price > upper
    if item["fresh_emitted"]:
        if fresh:
            return f"BREAKOUT_ACTIVE_{side}", False
        if outward:
            return f"BREAKOUT_RETRACING_{side}", False
        if inside or adverse:
            return "BREAKOUT_FAILED", False
    if fresh:
        return f"BREAKOUT_FRESH_{side}", True
    if outward:
        return f"BREAKOUT_UNCONFIRMED_{side}", False
    if adverse:
        return f"BOUNDARY_EXIT_ADVERSE_{side}", False
    return f"COMPRESSION_ACTIVE_{side}", False


def apply_live_prices(state: dict, prices: dict[str, float], now_ms: int) -> tuple[dict, list[dict]]:
    _validate_state(state)
    _require_nonnegative_int(now_ms, "now_ms")
    if not isinstance(prices, dict):
        raise ValueError("prices must be a dictionary")
    out, fresh_events = copy.deepcopy(state), []
    for compression_id, item in list(out["pool"].items()):
        price = prices.get(item.get("symbol"))
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(float(price)):
            if item.get("symbol") in prices:
                raise ValueError("live price must be finite")
            continue
        item["live_price"] = float(price)
        item["last_price_at"] = now_ms
        next_state, emits_fresh = _live_transition(item, float(price))
        if emits_fresh:
            item["fresh_emitted"] = True
            item["state"] = next_state
            fresh_events.append(_event(out, item, next_state, float(price), now_ms))
        else:
            item["state"] = next_state
        if next_state == "BREAKOUT_FAILED":
            out["episodes"][compression_id] = copy.deepcopy(item)
            del out["pool"][compression_id]
    _validate_state(out)
    return out, fresh_events


def write_compression_snapshot(directory: Path, evaluation: dict, frame: pd.DataFrame) -> str:
    if not isinstance(evaluation, dict) or not isinstance(evaluation.get("compression_id"), str) or not evaluation["compression_id"]:
        raise ValueError("evaluation compression_id is required")
    compression_id = evaluation["compression_id"]
    ref = f"momentum_compression_snapshots/{compression_id}.json"
    path = Path(directory) / ref
    if path.exists():
        return ref
    if not isinstance(frame, pd.DataFrame) or any(column not in frame for column in ("ot", "o", "h", "l", "c", "v")):
        raise ValueError("frame must contain OHLCV columns")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "parameter_version": evaluation.get("parameter_version", ""),
        "symbol": evaluation.get("symbol", ""),
        "side": evaluation.get("side", ""),
        "compression_id": compression_id,
        "compression_start_time": evaluation.get("compression_start_time"),
        "compression_end_time": evaluation.get("compression_end_time"),
        "ohlcv": {column: frame[column].tolist() for column in ("ot", "o", "h", "l", "c", "v")},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(encoded)
    return ref
