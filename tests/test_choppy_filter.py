import json
import logging
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import strategy_filters
from strategy_filters import evaluate_choppy_market_adaptive, is_choppy_market_adaptive
from trader import Position, SqueezeBreakoutBot, TradeConfig


def _frame(closes, half_range=1.0):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "ot": np.arange(len(closes), dtype=np.int64) * 1_800_000,
        "o": closes,
        "h": closes + half_range,
        "l": closes - half_range,
        "c": closes,
        "v": np.full(len(closes), 1000.0),
    })


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _dexe_frame():
    rows = json.loads(
        (FIXTURES / "dexeusdt_30m_20260718_signal.json").read_text(encoding="utf-8")
    )
    return pd.DataFrame(rows)


class PredictaWeakTrendChoppyTest(unittest.TestCase):
    def test_real_dexe_signal_is_weak_directional_chop(self):
        self.assertTrue(hasattr(strategy_filters, "evaluate_predicta_choppy_market"))
        state = strategy_filters.evaluate_predicta_choppy_market(_dexe_frame())

        self.assertTrue(state["choppy_filter_is_choppy"])
        self.assertIn("weak_directional_efficiency", state["choppy_filter_reasons"])
        self.assertLess(state["choppy_adx"], 18.0)
        self.assertLess(state["choppy_efficiency_ratio"], 0.20)

    def test_directional_expansion_remains_allowed(self):
        frame = _frame(100.0 + np.arange(130) * 0.5)

        state = strategy_filters.evaluate_predicta_choppy_market(frame)

        self.assertFalse(state["choppy_filter_is_choppy"])
        self.assertGreaterEqual(state["choppy_efficiency_ratio"], 0.20)

    def test_later_candles_do_not_change_signal_anchor_result(self):
        frame = _dexe_frame()
        extended = pd.concat([frame, _frame([80.0, 120.0, 70.0])], ignore_index=True)

        expected = strategy_filters.evaluate_predicta_choppy_market(frame)
        actual = strategy_filters.evaluate_predicta_choppy_market(
            extended, anchor_idx=len(frame) - 1
        )

        self.assertEqual(actual, expected)

    def test_weak_trend_requires_both_adx_and_efficiency(self):
        frame = _dexe_frame()

        adx_only = strategy_filters.evaluate_predicta_choppy_market(
            frame, efficiency_threshold=0.01
        )
        efficiency_only = strategy_filters.evaluate_predicta_choppy_market(
            frame, adx_threshold=1.0
        )

        self.assertNotIn("weak_directional_efficiency", adx_only["choppy_filter_reasons"])
        self.assertNotIn("weak_directional_efficiency", efficiency_only["choppy_filter_reasons"])

    def test_invalid_efficiency_keeps_existing_result(self):
        frame = _frame([100.0] * 130)

        state = strategy_filters.evaluate_predicta_choppy_market(frame)

        self.assertTrue(state["choppy_filter_is_choppy"])
        self.assertIsNone(state["choppy_efficiency_ratio"])
        self.assertNotIn("weak_directional_efficiency", state["choppy_filter_reasons"])

    def test_predicta_uses_enhanced_filter_while_rj_keeps_base_filter(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(
            predicta_choppy_filter_mode="hard", rj_choppy_filter_mode="hard"
        )
        frame = _dexe_frame()

        predicta = bot._predicta_choppy_filter_state(frame, len(frame) - 1)
        rj = bot._rj_choppy_filter_state(frame, len(frame) - 1)

        self.assertTrue(predicta["choppy_filter_is_choppy"])
        self.assertFalse(rj["choppy_filter_is_choppy"])

    def test_position_audit_keeps_predicta_direction_metrics(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)

        audit = bot._position_choppy_filter({
            "choppy_filter_mode": "hard",
            "choppy_filter_available": True,
            "choppy_filter_is_choppy": False,
            "choppy_filter_reason": "pass",
            "choppy_adx_period": 14,
            "choppy_adx": 24.5,
            "choppy_efficiency_period": 20,
            "choppy_efficiency_ratio": 0.31,
        })

        self.assertEqual(audit["adx_period"], 14)
        self.assertAlmostEqual(audit["adx"], 24.5)
        self.assertEqual(audit["efficiency_period"], 20)
        self.assertAlmostEqual(audit["efficiency_ratio"], 0.31)


class ChoppyFilterModuleTest(unittest.TestCase):
    def test_flat_box_is_classified_without_mutating_frame(self):
        frame = _frame([100.0] * 130)
        original = frame.copy(deep=True)

        state = evaluate_choppy_market_adaptive(frame)

        self.assertTrue(state["choppy_filter_available"])
        self.assertTrue(state["choppy_filter_is_choppy"])
        self.assertIn("box_squeeze", state["choppy_filter_reasons"])
        self.assertIn("middle_chop", state["choppy_filter_reasons"])
        pd.testing.assert_frame_equal(frame, original)

    def test_atr_contraction_uses_symbol_own_baseline(self):
        frame = _frame([100.0] * 130, half_range=2.0)
        frame.loc[110:, "h"] = 100.2
        frame.loc[110:, "l"] = 99.8

        state = evaluate_choppy_market_adaptive(frame)

        self.assertIn("atr_contraction", state["choppy_filter_reasons"])
        self.assertLess(state["choppy_atr_ratio"], 0.70)

    def test_directional_expansion_is_not_choppy(self):
        frame = _frame(100.0 + np.arange(130) * 0.5)

        state = evaluate_choppy_market_adaptive(frame)

        self.assertTrue(state["choppy_filter_available"])
        self.assertFalse(state["choppy_filter_is_choppy"])
        self.assertFalse(is_choppy_market_adaptive(frame))

    def test_anchor_ignores_later_candles(self):
        base = _frame(100.0 + np.arange(130) * 0.5)
        extended = pd.concat([base, _frame([50.0] * 20)], ignore_index=True)

        expected = evaluate_choppy_market_adaptive(base, anchor_idx=129)
        actual = evaluate_choppy_market_adaptive(extended, anchor_idx=129)

        self.assertEqual(actual, expected)

    def test_insufficient_history_is_explicit_and_fail_open(self):
        state = evaluate_choppy_market_adaptive(_frame([100.0] * 100))

        self.assertFalse(state["choppy_filter_available"])
        self.assertFalse(state["choppy_filter_is_choppy"])
        self.assertEqual(state["choppy_filter_reason"], "insufficient_data")


class ChoppyFilterRjIntegrationTest(unittest.TestCase):
    def _bot(self, mode):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig()
        bot.cfg.min_score = 0
        bot.cfg.rj_min_jr_spread = 0
        bot.cfg.rj_only_stats_enabled = False
        bot.cfg.rj_choppy_filter_mode = mode
        bot._log = logging.getLogger("test_choppy_filter")

        def compute_lines(frame):
            j_line = pd.Series([-1.0] * (len(frame) - 1) + [1.0])
            r_line = pd.Series([0.0] * len(frame))
            return {"j": j_line, "r": r_line, "params": {}}

        bot._compute_rj_lines = compute_lines
        bot._rj_original_level_triggers = lambda line: (
            pd.Series([False] * len(line)), pd.Series([False] * len(line)),
        )
        bot._calc_atr = lambda frame, period: 2.0
        bot._rj_only_volume_state = lambda frame, index, anchor: {"rj_volume_filter_pass": True}
        bot._rj_only_sr_divergence_state = lambda frame, line, direction, index: {"rj_sr_filter_pass": True}
        bot._rj_only_history_stats = lambda frame, direction: {}
        bot._score_rj_only_signal = lambda signal: 80.0
        return bot

    def test_log_only_mode_attaches_shadow_state_without_blocking_setup(self):
        setup = self._bot("log_only")._rj_only_setup_from_df(
            "TESTUSDT", "30m", _frame([100.0] * 130)
        )

        self.assertIsNotNone(setup)
        self.assertEqual(setup["choppy_filter_mode"], "log_only")
        self.assertTrue(setup["choppy_filter_is_choppy"])
        self.assertEqual(setup["choppy_filter_anchor"], "signal_key")

    def test_hard_mode_blocks_choppy_setup(self):
        setup = self._bot("hard")._rj_only_setup_from_df(
            "TESTUSDT", "30m", _frame([100.0] * 130)
        )

        self.assertIsNone(setup)

    def test_demo_pool_writes_shadow_event_for_choppy_setup(self):
        bot = self._bot("log_only")
        setup = bot._rj_only_setup_from_df("TESTUSDT", "30m", _frame([100.0] * 130))
        events = []
        bot._rj_setup_pool = {}
        bot.positions = []
        bot._is_live_trade_symbol = lambda symbol: True
        bot._any_signal_key_used = lambda keys: False
        bot._rj_setup_expiry_ts = lambda item, now: time.time() + 3600
        bot._format_setup_ts = lambda value: str(value)
        bot._btc_regime_fields = lambda: {}
        bot._append_signal_event = lambda event, symbol="", payload=None: events.append(event)

        bot._sync_rj_setup_pool([setup], {})

        self.assertIn("rj_setup_add", events)
        self.assertIn("rj_choppy_shadow", events)


class ChoppyFilterPositionAuditTest(unittest.TestCase):
    def _position(self):
        return Position(
            symbol="TESTUSDT",
            direction="LONG",
            entry_price=100.0,
            entry_time=datetime(2026, 7, 14, 20, 0),
            quantity=1.0,
            sl_price=98.0,
            current_sl=98.0,
            risk_usdt=2.0,
            signal_score=75.0,
            initial_band_hi=100.0,
            initial_band_lo=100.0,
            choppy_filter={
                "available": True,
                "is_choppy": True,
                "reason": "middle_chop",
                "reasons": ["middle_chop"],
                "atr_ratio": 0.82,
                "box_position": 0.51,
                "box_amplitude": 0.03,
            },
        )

    def test_position_audit_is_saved_and_restored(self):
        bot = SqueezeBreakoutBot(TradeConfig(mode="paper", enabled=False, exchange="bitget"))
        with tempfile.TemporaryDirectory() as tmp:
            bot._positions_path = str(Path(tmp) / "positions.json")
            bot.positions = [self._position()]

            bot._save_positions()
            restored = bot._load_positions()

        self.assertEqual(len(restored), 1)
        self.assertTrue(restored[0].choppy_filter["is_choppy"])
        self.assertEqual(restored[0].choppy_filter["reason"], "middle_chop")
        self.assertAlmostEqual(restored[0].choppy_filter["atr_ratio"], 0.82)

    def test_position_audit_uses_entry_signal_snapshot(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        audit = bot._position_choppy_filter({
            "choppy_filter_mode": "log_only",
            "choppy_filter_available": True,
            "choppy_filter_is_choppy": False,
            "choppy_filter_reason": "pass",
            "choppy_filter_reasons": [],
            "choppy_filter_anchor": "signal_key",
            "choppy_atr_ratio": 1.12,
            "choppy_box_position": 0.74,
            "choppy_box_amplitude": 0.04,
        })

        self.assertTrue(audit["recorded"])
        self.assertEqual(audit["mode"], "log_only")
        self.assertEqual(audit["anchor"], "signal_key")
        self.assertFalse(audit["is_choppy"])
        self.assertAlmostEqual(audit["box_position"], 0.74)

    def test_explicit_unrecorded_audit_stays_unrecorded(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        first = bot._position_choppy_filter({})
        second = bot._position_choppy_filter(first)
        migrated = bot._position_choppy_filter({**first, "recorded": True})

        self.assertFalse(first["recorded"])
        self.assertFalse(second["recorded"])
        self.assertFalse(migrated["recorded"])
        self.assertEqual(second["reason"], "not_recorded")
        self.assertIsNone(second["adx"])
        self.assertIsNone(second["efficiency_ratio"])


if __name__ == "__main__":
    unittest.main()
