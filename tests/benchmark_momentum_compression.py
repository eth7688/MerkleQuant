import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momentum_compression import evaluate_both_sides


BASE_OT = 1_700_000_000_000


def frame_for(seed, bars=220):
    rng = np.random.default_rng(seed)
    indexes = np.arange(bars, dtype=float)
    pulse = np.sin(indexes * np.pi / 3) + rng.normal(0.0, 0.03, bars)
    return pd.DataFrame({
        "ot": BASE_OT + indexes.astype(int) * 900_000,
        "o": 100.0 + 0.4 * indexes,
        "h": 105.0 + 0.3 * indexes + pulse,
        "l": 95.0 + 0.5 * indexes + pulse,
        "c": 100.0 + 0.4 * indexes,
        "v": np.full(bars, 1000.0),
    })


def main():
    frames = [frame_for(seed) for seed in range(500)]
    evaluated_at = int(frames[0]["ot"].iloc[-1] + 900_000)
    started = time.perf_counter()
    for index, frame in enumerate(frames):
        evaluate_both_sides(
            f"TEST{index}USDT", frame, float(frame["c"].iloc[-1]),
            evaluated_at_ms=evaluated_at,
            htf_alignment_by_side={},
        )
    elapsed = time.perf_counter() - started
    print(f"500-symbol offline calculation: {elapsed:.3f}s")
    return 0 if elapsed <= 300.0 else 1


if __name__ == "__main__":
    sys.exit(main())
