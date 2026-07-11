import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_core import (
    ExitRules,
    PositionState,
    StrategySnapshot,
    advance_position,
    evaluate_rj_entry,
)


class FakeBot:
    def __init__(self, signal=None):
        self.signal = signal
        self.calls = []

    def _rj_only_signal_from_df(self, symbol, interval, candles):
        self.calls.append((symbol, interval, len(candles)))
        return self.signal


def candles():
    return pd.DataFrame({
        "ot": [0, 1_800_000, 3_600_000],
        "o": [100, 101, 102],
        "h": [101, 102, 104],
        "l": [99, 100, 101],
        "c": [100.5, 101.5, 103],
        "v": [1000, 1200, 1500],
    })


class StrategyCoreTest(unittest.TestCase):
    def test_entry_adapter_reuses_live_rj_decision(self):
        signal = {
            "direction": "LONG",
            "price": 103.0,
            "rj_only_stop_price": 99.0,
            "rj_only_key_time": 1_800_000,
            "rj_only_confirm_time": 3_600_000,
            "rj_trigger_source": "j0_recover",
            "signal_key": "RJ|TEST|LONG",
        }
        bot = FakeBot(signal)
        snapshot = StrategySnapshot("TESTUSDT", "30m", 5_400_000, candles(), {})

        decision = evaluate_rj_entry(bot, snapshot)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.direction, "LONG")
        self.assertEqual(decision.stop, 99.0)
        self.assertEqual(decision.signal_key, "RJ|TEST|LONG")
        self.assertEqual(bot.calls, [("TESTUSDT", "30m", 3)])

    def test_entry_rejects_future_or_unclosed_candle(self):
        bot = FakeBot({"direction": "LONG"})
        snapshot = StrategySnapshot("TESTUSDT", "30m", 3_000_000, candles(), {})

        with self.assertRaisesRegex(ValueError, "strategy_snapshot_lookahead"):
            evaluate_rj_entry(bot, snapshot)

        self.assertEqual(bot.calls, [])

    def test_extreme_btc_veto_is_applied_after_live_signal(self):
        signal = {
            "direction": "SHORT",
            "price": 100.0,
            "rj_only_stop_price": 102.0,
            "signal_key": "RJ|TEST|SHORT",
        }
        stage = {"stage": "early_bull", "direction": "bull", "extreme_veto": True}
        snapshot = StrategySnapshot("TESTUSDT", "30m", 5_400_000, candles(), stage)

        decision = evaluate_rj_entry(FakeBot(signal), snapshot)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "btc_extreme_bull_blocks_short")

    def test_entry_applies_live_stats_and_score_gates(self):
        signal = {
            "direction": "LONG", "price": 103.0, "rj_only_stop_price": 99.0,
            "signal_key": "RJ|TEST|LONG", "score": 80.0,
            "rj_only_stats_pass": False, "rj_only_stats_reason": "hist_win_rate_low",
        }
        bot = FakeBot(signal)
        bot.cfg = SimpleNamespace(rj_only_stats_enabled=True, min_score=70.0)
        snapshot = StrategySnapshot("TESTUSDT", "30m", 5_400_000, candles(), {})

        decision = evaluate_rj_entry(bot, snapshot)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "hist_win_rate_low")

    def test_exit_state_machine_protects_then_partially_exits(self):
        position = PositionState("TESTUSDT", "LONG", 100.0, 98.0, 1.0)
        rules = ExitRules(early_protect_r=0.8, early_lock_r=0.0, tier1_r=1.2, tier2_r=1.7)

        position, first = advance_position(position, {"ot": 1, "o": 100, "h": 101.8, "l": 100, "c": 101.5}, rules)
        self.assertEqual([event.action for event in first], ["move_stop"])
        self.assertEqual(position.current_stop, 100.0)

        position, second = advance_position(position, {"ot": 2, "o": 101.5, "h": 103.6, "l": 101.2, "c": 103.4}, rules)
        self.assertIn("partial_exit", [event.action for event in second])
        self.assertAlmostEqual(position.remaining_qty, 0.5)

    def test_stop_is_adverse_first_inside_same_bar(self):
        position = PositionState("TESTUSDT", "LONG", 100.0, 98.0, 1.0)

        position, events = advance_position(
            position,
            {"ot": 1, "o": 100, "h": 104.0, "l": 97.5, "c": 103.0},
            ExitRules(),
        )

        self.assertEqual(events[0].action, "full_exit")
        self.assertEqual(events[0].reason, "stop")
        self.assertEqual(position.remaining_qty, 0.0)


if __name__ == "__main__":
    unittest.main()
