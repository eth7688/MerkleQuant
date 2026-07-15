import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from predicta_indicator import PredictaParams, make_predicta_setup
from trader import SqueezeBreakoutBot, TradeConfig


def _frame(size=120):
    close = np.linspace(100.0, 110.0, size)
    return pd.DataFrame({
        "ot": np.arange(size, dtype=np.int64) * 1_800_000,
        "o": close - 0.1,
        "h": close + 0.5,
        "l": close - 0.5,
        "c": close,
        "v": np.full(size, 1000.0),
    })


def _lines(frame, ewo):
    result = frame.copy()
    result["bull_signal"] = False
    result["bear_signal"] = False
    result["ewo"] = 0.0
    result.loc[len(result) - 1, "bull_signal"] = True
    result.loc[len(result) - 1, "ewo"] = ewo
    return result


class PredictaPipelineTest(unittest.TestCase):
    def setUp(self):
        self.bot = object.__new__(SqueezeBreakoutBot)
        self.bot.cfg = TradeConfig(entry_signal_source="predicta_ewo")
        self.bot._log = __import__("logging").getLogger("predicta-test")

    def test_aligned_signal_key_uses_fast_path_only(self):
        frame = _frame()
        with patch("trader.compute_predicta", return_value=_lines(frame, 1.0)), patch.object(
            self.bot, "_predicta_choppy_filter_state", return_value={
                "choppy_filter_mode": "hard", "choppy_filter_is_choppy": False,
            },
        ):
            fast, waiting = self.bot._predicta_candidates_from_df("BTCUSDT", "30m", frame)

        self.assertEqual(len(fast), 1)
        self.assertEqual(waiting, [])
        self.assertEqual(fast[0]["predicta_entry_path"], "fast")
        self.assertEqual(fast[0]["source_strategy"], "predicta_ewo")
        self.assertGreater(fast[0]["predicta_stop_price"], 0)

    def test_opposite_ewo_signal_key_enters_wait_pool(self):
        frame = _frame()
        with patch("trader.compute_predicta", return_value=_lines(frame, -1.0)), patch.object(
            self.bot, "_predicta_choppy_filter_state", return_value={
                "choppy_filter_mode": "hard", "choppy_filter_is_choppy": False,
            },
        ):
            fast, waiting = self.bot._predicta_candidates_from_df("BTCUSDT", "30m", frame)

        self.assertEqual(fast, [])
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["predicta_entry_path"], "wait")

    def test_hard_choppy_filter_blocks_signal_key(self):
        frame = _frame()
        with patch("trader.compute_predicta", return_value=_lines(frame, 1.0)), patch.object(
            self.bot, "_predicta_choppy_filter_state", return_value={
                "choppy_filter_mode": "hard", "choppy_filter_is_choppy": True,
            },
        ):
            fast, waiting = self.bot._predicta_candidates_from_df("BTCUSDT", "30m", frame)

        self.assertEqual(fast, [])
        self.assertEqual(waiting, [])

    def test_wait_pool_confirmation_uses_confirmation_candle_atr(self):
        frame = _frame(120)
        frame[["o", "h", "l", "c"]] = [99.9, 100.5, 99.5, 100.0]
        signal_index = len(frame) - 2
        frame.loc[signal_index, ["o", "h", "l", "c"]] = [100.0, 100.5, 99.5, 100.0]
        frame.loc[signal_index + 1, ["o", "h", "l", "c"]] = [100.0, 103.0, 99.8, 102.5]
        setup = make_predicta_setup(
            "BTCUSDT", "LONG", "30m", frame, signal_index, -1.0, {}, PredictaParams()
        )

        signal = self.bot._predicta_confirmed_signal(setup, frame)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["predicta_entry_path"], "wait")
        self.assertEqual(signal["predicta_confirm_time"], int(frame.iloc[-1]["ot"]))
        self.assertGreater(signal["predicta_atr"], 0)
        self.assertEqual(signal["source_strategy"], "predicta_ewo")

    def test_scanner_requests_closed_candles_only(self):
        self.bot.positions = []
        self.bot._predicta_latest_setups = []
        seen = []

        def fake_klines(symbol, interval, limit, exchange, closed_only=False):
            seen.append(closed_only)
            return _frame(120)

        fast_signal = {"symbol": "BTCUSDT", "score": 100.0, "signal_key": "fast"}
        with patch("trader.fetch_pairs", return_value=(["BTCUSDT"], {"BTCUSDT": 9_000_000})), patch(
            "trader.fetch_klines", side_effect=fake_klines
        ), patch.object(
            self.bot, "_filter_live_trade_symbols", side_effect=lambda rows: rows
        ), patch.object(
            self.bot, "_predicta_candidates_from_df", return_value=([fast_signal], [])
        ):
            signals = self.bot._scan_predicta_signals(["30m"], {})

        self.assertEqual(signals, [fast_signal])
        self.assertTrue(seen)
        self.assertTrue(all(seen))

    def test_predicta_entry_uses_shared_risk_engine_without_rj_filters(self):
        bot = SqueezeBreakoutBot(TradeConfig(
            mode="paper", enabled=False, exchange="bitget",
            entry_signal_source="predicta_ewo", min_score=70.0,
        ))
        frame = _frame(160)
        signal = {
            "symbol": "BTCUSDT", "direction": "LONG", "price": 110.0,
            "score": 100.0, "source_interval": "30m",
            "source_strategy": "predicta_ewo",
            "signal_key": "PREDICTA|BTCUSDT|LONG|30m|1",
            "predicta_stop_price": 105.0,
            "predicta_entry_path": "fast",
            "choppy_filter_mode": "hard", "choppy_filter_is_choppy": False,
        }

        with patch("trader.fetch_klines", return_value=frame), patch.object(
            bot, "_save_positions"
        ), patch.object(bot, "_append_signal_event"):
            position = bot.enter_predicta_position(signal)

        self.assertIsNotNone(position)
        self.assertEqual(position.source_strategy, "predicta_ewo")
        self.assertEqual(position.signal_key, signal["signal_key"])
        self.assertEqual(position.initial_sl, 105.0)


if __name__ == "__main__":
    unittest.main()
