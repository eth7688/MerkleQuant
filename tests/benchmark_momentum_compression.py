import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momentum_compression import evaluate_both_sides


BASE_OT = 1_700_000_000_000
PUBLIC_AUDIT_FIELDS = {
    "symbol", "side", "state", "evaluated_at", "htf_alignment", "parameter_version",
    "rejection_reasons", "compression_bars", "compression_start_time",
    "compression_end_time", "upper_boundary_price", "lower_boundary_price", "atr14",
    "breakout_buffer_price", "directional_touch_times", "score_components", "compression_id",
}


def frame_for(seed, bars=220):
    """Deterministic, valid OHLCV path; seed controls cohort and geometry."""
    indexes = np.arange(bars, dtype=float)
    deep = seed < 100
    side = "LONG" if seed % 2 == 0 else "SHORT"
    direction = 1.0 if side == "LONG" else -1.0
    slope = 0.09 + 0.002 * (seed % 5)
    close = 100.0 + 0.01 * (seed % 17) + direction * slope * indexes
    phase = 0.1 * (seed % 11)
    pulse = (0.7 + 0.03 * (seed % 5)) * np.sin(indexes * np.pi / 3 + phase)
    half_width = 5.1 + 0.04 * (seed % 7) - (0.014 + 0.0002 * (seed % 3)) * indexes
    frame = pd.DataFrame({
        "ot": BASE_OT + indexes.astype(int) * 900_000,
        "o": close,
        "h": close + half_width + pulse,
        "l": close - half_width + pulse,
        "c": close,
        "v": np.full(bars, 1000.0 + seed),
    })
    anomaly_index = 20 + (seed % 3) if deep else 130 + (seed % 5)
    anomaly_size = 20.0 if deep else 10.0 + 0.1 * (seed % 3)
    if side == "LONG":
        frame.loc[anomaly_index, "h"] += anomaly_size
    else:
        frame.loc[anomaly_index, "l"] -= anomaly_size
    return frame, {"cohort": "deep" if deep else "normal", "intended_side": side}


def _validate_rows(rows):
    if len(rows) != 2 or [row["side"] for row in rows] != ["LONG", "SHORT"]:
        raise AssertionError(f"expected LONG then SHORT rows, got {rows!r}")
    for row in rows:
        missing = PUBLIC_AUDIT_FIELDS.difference(row)
        if missing:
            raise AssertionError(f"missing public audit fields: {sorted(missing)}")


def run_benchmark():
    records = []
    started = time.perf_counter()
    for seed in range(500):
        frame, metadata = frame_for(seed)
        if not (frame["h"] >= frame[["o", "c"]].max(axis=1)).all():
            raise AssertionError(f"invalid high at seed {seed}")
        if not (frame["l"] <= frame[["o", "c"]].min(axis=1)).all():
            raise AssertionError(f"invalid low at seed {seed}")
        rows = evaluate_both_sides(
            f"TEST{seed}USDT", frame, float(frame["c"].iloc[-1]),
            evaluated_at_ms=int(frame["ot"].iloc[-1] + 900_000),
            htf_alignment_by_side={},
        )
        _validate_rows(rows)
        records.extend({**metadata, "seed": seed, "row": row} for row in rows)
    elapsed = time.perf_counter() - started
    states = Counter(record["row"]["state"] for record in records)
    rejections = Counter(
        reason for record in records for reason in record["row"]["rejection_reasons"]
    )
    deep_intended = [
        record for record in records
        if record["cohort"] == "deep" and record["row"]["side"] == record["intended_side"]
    ]
    deep_window_too_long = sum(
        "WINDOW_TOO_LONG" in record["row"]["rejection_reasons"] for record in deep_intended
    )
    normal_active_sides = {
        record["row"]["side"] for record in records
        if record["cohort"] == "normal" and not record["row"]["rejection_reasons"]
    }
    if len(deep_intended) != 100 or deep_window_too_long != 100:
        raise AssertionError(
            f"deep traversal evidence missing: intended={len(deep_intended)} "
            f"window_too_long={deep_window_too_long}"
        )
    if normal_active_sides != {"LONG", "SHORT"}:
        raise AssertionError(f"normal cases did not exercise active both-side paths: {normal_active_sides}")
    return elapsed, {
        "records": len(records),
        "states": dict(sorted(states.items())),
        "rejections": dict(sorted(rejections.items())),
        "deep_intended_rows": len(deep_intended),
        "deep_window_too_long": deep_window_too_long,
        "normal_active_sides": sorted(normal_active_sides),
    }


def main():
    elapsed, summary = run_benchmark()
    print(f"500-symbol offline calculation: {elapsed:.3f}s")
    print(f"result/rejection summary: {summary}")
    return 0 if elapsed <= 300.0 else 1


if __name__ == "__main__":
    sys.exit(main())
