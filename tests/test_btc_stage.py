import unittest

import numpy as np
import pandas as pd

from btc_stage import (
    build_interval_snapshot,
    classify_stage_snapshots,
    evaluate_btc_gate,
)


def make_frame(direction="up", bars=160, exhausted=False):
    idx = np.arange(bars, dtype=float)
    if direction == "up":
        close = 100.0 + idx * 0.35 + np.sin(idx / 5.0) * 0.3
    elif direction == "down":
        close = 160.0 - idx * 0.35 + np.sin(idx / 5.0) * 0.3
    else:
        close = 100.0 + np.sin(idx / 4.0) * 0.2
    if exhausted:
        close[-12:] = close[-13] + np.linspace(0.1, 5.0, 12)
    volume = np.full(bars, 1000.0)
    if direction != "flat":
        volume[-20:] = np.linspace(900.0, 1500.0, 20)
    if exhausted:
        volume[-12:] = np.linspace(1600.0, 400.0, 12)
    return pd.DataFrame({
        "ot": (idx.astype("int64") * 3_600_000),
        "o": close - 0.1,
        "h": close + 0.5,
        "l": close - 0.5,
        "c": close,
        "v": volume,
    })


def snapshot(direction, **overrides):
    base = {
        "direction": direction,
        "ema20_slope_atr": 0.18 if direction == "bull" else -0.18,
        "ema60_slope_atr": 0.08 if direction == "bull" else -0.08,
        "ema_spread_atr": 1.2 if direction == "bull" else -1.2,
        "ema_spread_change": 0.25,
        "adx": 34.0,
        "adx_change": 4.0,
        "distance_ema20_atr": 1.1 if direction == "bull" else -1.1,
        "volume_ratio": 1.25,
        "breakout_age": 2,
        "momentum_change": 0.2 if direction == "bull" else -0.2,
        "exhaustion_flags": [],
        "closed_at": 1_000,
    }
    base.update(overrides)
    return base


class BtcStageTest(unittest.TestCase):
    def test_build_interval_snapshot_uses_last_closed_row(self):
        row = build_interval_snapshot(make_frame("up"))

        self.assertEqual(row["direction"], "bull")
        self.assertEqual(row["closed_at"], 159 * 3_600_000)
        self.assertGreater(row["atr"], 0)
        self.assertGreater(row["adx"], 0)
        self.assertIn("distance_ema20_atr", row)

    def test_requires_enough_closed_bars(self):
        with self.assertRaisesRegex(ValueError, "btc_stage_insufficient_bars"):
            build_interval_snapshot(make_frame("up", bars=50))

    def test_classifies_aligned_expanding_bull_as_extreme_early(self):
        result = classify_stage_snapshots(snapshot("bull"), snapshot("bull"))

        self.assertEqual(result["stage"], "early_bull")
        self.assertTrue(result["extreme_veto"])
        self.assertEqual(result["rule_version"], "btc_stage_v1")

    def test_classifies_overextended_bull_as_late_without_veto(self):
        result = classify_stage_snapshots(
            snapshot("bull", distance_ema20_atr=3.2, exhaustion_flags=["overextended"]),
            snapshot("bull", distance_ema20_atr=2.7, exhaustion_flags=["momentum_divergence"]),
        )

        self.assertEqual(result["stage"], "late_bull")
        self.assertFalse(result["extreme_veto"])

    def test_classifies_weakening_bull_as_decay_without_veto(self):
        result = classify_stage_snapshots(
            snapshot("bull", adx=22, adx_change=-5, ema_spread_change=-0.2),
            snapshot("bull", adx=24, adx_change=-3, ema_spread_change=-0.1),
        )

        self.assertEqual(result["stage"], "bull_decay")
        self.assertFalse(result["extreme_veto"])

    def test_mixed_timeframes_are_range_and_do_not_veto(self):
        result = classify_stage_snapshots(snapshot("bull"), snapshot("bear"))

        self.assertEqual(result["stage"], "range")
        self.assertFalse(result["extreme_veto"])

    def test_extreme_veto_has_no_coin_exception(self):
        stage = classify_stage_snapshots(snapshot("bull"), snapshot("bull"))

        allowed, reason = evaluate_btc_gate("SHORT", stage, coin_reversal_pass=True)

        self.assertFalse(allowed)
        self.assertEqual(reason, "btc_extreme_bull_blocks_short")

    def test_ordinary_mid_trend_requires_coin_reversal_package(self):
        stage = {"stage": "mid_bull", "direction": "bull", "extreme_veto": False}

        self.assertEqual(
            evaluate_btc_gate("SHORT", stage, coin_reversal_pass=False),
            (False, "btc_bull_opposite_requires_coin_reversal"),
        )
        self.assertEqual(
            evaluate_btc_gate("SHORT", stage, coin_reversal_pass=True),
            (True, "btc_opposite_coin_reversal_pass"),
        )

    def test_late_and_unknown_states_pass(self):
        late = {"stage": "late_bear", "direction": "bear", "extreme_veto": False}
        unknown = {"stage": "unknown", "direction": "unknown", "extreme_veto": False}

        self.assertEqual(evaluate_btc_gate("LONG", late), (True, "pass"))
        self.assertEqual(evaluate_btc_gate("SHORT", unknown), (True, "btc_unknown_pass"))


if __name__ == "__main__":
    unittest.main()
