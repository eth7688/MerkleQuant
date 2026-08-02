"""Backfill missing demo daily-pattern snapshots without entry-time lookahead."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screener import fetch_klines
from strategy_filters import evaluate_daily_pattern_state


DEFAULT_CUTOFF = "2026-08-01T08:10:32+00:00"


def parse_time_ms(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def build_entry_time_index(event_lines: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for raw in event_lines:
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("event") != "entry_filled":
            continue
        snapshot = event.get("snapshot") or event.get("data") or {}
        key = str(event.get("signal_key") or snapshot.get("signal_key") or "").strip()
        entry_ms = parse_time_ms(event.get("time"))
        if not key or entry_ms is None:
            continue
        if key in index and index[key] != entry_ms:
            raise RuntimeError(f"non-unique entry event for {key}")
        index[key] = entry_ms
    return index


def _entry_time_for_row(
    kind: str,
    row: dict,
    entry_times: dict[str, int],
    cutoff_ms: int,
) -> int | None:
    if kind == "position":
        return parse_time_ms(row.get("entry_time"))
    key = str(row.get("signal_key", "") or "").strip()
    if key and key in entry_times:
        return entry_times[key]
    exit_ms = parse_time_ms(row.get("time"))
    if exit_ms is not None and exit_ms >= cutoff_ms:
        raise RuntimeError(
            f"recent trade has no unique entry event: {row.get('symbol')}/{key}"
        )
    return None


def _eligible(row: dict, entry_ms: int | None, cutoff_ms: int) -> tuple[bool, int | None]:
    pattern = row.get("daily_pattern") or {}
    reason = str(pattern.get("reason", "not_recorded") or "not_recorded")
    return (
        entry_ms is not None
        and entry_ms >= cutoff_ms
        and not bool(pattern.get("recorded"))
        and reason in ("", "not_recorded")
    ), entry_ms


def _snapshot(row: dict, entry_ms: int, daily: pd.DataFrame) -> dict:
    state = evaluate_daily_pattern_state(
        daily,
        str(row.get("direction", "")),
        decision_time=entry_ms,
    )
    if not state.get("recorded"):
        raise RuntimeError(
            f"{row.get('symbol')} at {entry_ms}: {state.get('reason', 'unavailable')}"
        )
    state["mode"] = "log_only"
    return state


def backfill_records(
    positions: list[dict],
    trade_lines: list[str],
    entry_times: dict[str, int],
    cutoff_ms: int,
    history_loader: Callable[[str], pd.DataFrame],
) -> tuple[list[dict], list[str], dict]:
    new_positions = copy.deepcopy(positions)
    parsed_trades = [
        (line, json.loads(line) if line.strip() else None)
        for line in trade_lines
    ]
    targets = []
    skipped_pre_cutoff = 0
    skipped_recorded = 0
    for kind, indexed_rows in (
        ("position", enumerate(new_positions)),
        ("trade", (
            (index, row)
            for index, (_, row) in enumerate(parsed_trades)
            if row is not None
        )),
    ):
        for index, row in indexed_rows:
            entry_ms = _entry_time_for_row(kind, row, entry_times, cutoff_ms)
            eligible, entry_ms = _eligible(row, entry_ms, cutoff_ms)
            if eligible:
                targets.append((kind, index, row, entry_ms))
                continue
            pattern = row.get("daily_pattern") or {}
            if bool(pattern.get("recorded")):
                skipped_recorded += 1
            elif entry_ms is not None and entry_ms < cutoff_ms:
                skipped_pre_cutoff += 1

    histories: dict[str, pd.DataFrame] = {}
    snapshots = []
    planned = []
    for kind, index, row, entry_ms in targets:
        symbol = str(row.get("symbol", "")).strip()
        direction = str(row.get("direction", "")).strip().upper()
        if not symbol or direction not in ("LONG", "SHORT"):
            raise RuntimeError(f"invalid target record: {symbol}/{direction}")
        if symbol not in histories:
            histories[symbol] = history_loader(symbol)
        state = _snapshot(row, entry_ms, histories[symbol])
        planned.append((kind, index, state))
        snapshots.append({
            "kind": kind,
            "symbol": symbol,
            "entry_time": entry_ms,
            "daily_pattern": state,
        })

    updated_positions = 0
    updated_trades = 0
    for kind, index, state in planned:
        if kind == "position":
            new_positions[index]["daily_pattern"] = state
            updated_positions += 1
        else:
            parsed_trades[index][1]["daily_pattern"] = state
            updated_trades += 1
    changed_trade_indexes = {
        index for kind, index, _ in planned if kind == "trade"
    }
    new_lines = [
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if index in changed_trade_indexes else raw
        for index, (raw, row) in enumerate(parsed_trades)
    ]
    report = {
        "targets": len(targets),
        "updated_positions": updated_positions,
        "updated_trades": updated_trades,
        "skipped_pre_cutoff": skipped_pre_cutoff,
        "skipped_recorded": skipped_recorded,
        "failures": 0,
        "snapshots": snapshots,
    }
    return new_positions, new_lines, report


def load_history(symbol: str) -> pd.DataFrame:
    return fetch_klines(
        symbol,
        "1d",
        50,
        exchange="bitget",
        closed_only=True,
        bitget_granularity="1Dutc",
    )


def atomic_write(path: Path, text: str) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill demo daily-pattern snapshots at their entry times.",
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    cutoff_ms = parse_time_ms(args.cutoff)
    if cutoff_ms is None:
        parser.error(f"invalid --cutoff: {args.cutoff}")

    root = args.root.resolve()
    positions_path = root / "positions_<uid>.json"
    trades_path = root / "trades_<uid>.jsonl"
    events_path = root / "signal_events_0.jsonl"
    paths = (positions_path, trades_path, events_path)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        parser.error("required paths do not exist: " + ", ".join(missing))

    positions = json.loads(positions_path.read_text(encoding="utf-8"))
    trade_lines = trades_path.read_text(encoding="utf-8").splitlines()
    event_lines = events_path.read_text(encoding="utf-8").splitlines()
    entry_times = build_entry_time_index(event_lines)
    new_positions, new_trade_lines, report = backfill_records(
        positions,
        trade_lines,
        entry_times,
        cutoff_ms,
        load_history,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not args.apply:
        return
    if report["targets"] <= 0:
        parser.error("--apply requires at least one target")

    positions_text = json.dumps(new_positions, ensure_ascii=False, indent=2) + "\n"
    trades_text = "\n".join(new_trade_lines) + "\n"
    atomic_write(positions_path, positions_text)
    atomic_write(trades_path, trades_text)


if __name__ == "__main__":
    main()
