import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screener import fetch_klines
from trader import Position, SqueezeBreakoutBot, TradeConfig


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return [[0, "1", "2", "0.5", "1.5", "10", 1, "15", 1, "5", "7", "0"]]


class BinanceKlineRoutingTest(unittest.TestCase):
    def test_fetch_klines_keeps_legacy_none_on_request_failure(self):
        with patch("screener.requests.get", side_effect=TimeoutError("slow")):
            self.assertIsNone(fetch_klines("DOTUSDT", "15m", 2))

    def test_fetch_klines_can_propagate_request_failure(self):
        with patch("screener.requests.get", side_effect=TimeoutError("slow")):
            with self.assertRaisesRegex(TimeoutError, "slow"):
                fetch_klines("DOTUSDT", "15m", 2, raise_errors=True)

    def _requested_url(self, **kwargs):
        with patch("screener.requests.get", return_value=FakeResponse()) as get:
            frame = fetch_klines("DOTUSDT", "30m", 1, closed_only=False, **kwargs)
        self.assertIsNotNone(frame)
        return get.call_args.args[0]

    def test_routes_klines_to_the_configured_binance_market(self):
        self.assertEqual(
            self._requested_url(exchange="binance", market_type="spot", testnet=False),
            "https://api.binance.com/api/v3/klines",
        )
        self.assertEqual(
            self._requested_url(exchange="binance", market_type="futures", testnet=False),
            "https://fapi.binance.com/fapi/v1/klines",
        )
        self.assertEqual(
            self._requested_url(exchange="binance", market_type="futures", testnet=True),
            "https://testnet.binancefuture.com/fapi/v1/klines",
        )
        self.assertEqual(
            self._requested_url(
                exchange="binance",
                market_type="futures",
                testnet=True,
                price_type="mark",
            ),
            "https://testnet.binancefuture.com/fapi/v1/markPriceKlines",
        )

    def test_exit_check_requests_klines_from_the_execution_market(self):
        cfg = TradeConfig(
            mode="paper",
            enabled=False,
            exchange="binance",
            market_type="futures",
            testnet=True,
            enable_time_stop=False,
        )
        bot = SqueezeBreakoutBot(cfg)
        position = Position(
            symbol="DOTUSDT",
            direction="SHORT",
            entry_price=0.832,
            entry_time=datetime.now(timezone.utc),
            quantity=100.0,
            sl_price=0.84285714,
            current_sl=0.84285714,
            risk_usdt=1.0,
            signal_score=100.0,
            initial_sl=0.84285714,
            initial_band_hi=0.84,
            initial_band_lo=0.83,
        )

        with patch("trader.fetch_klines", return_value=None) as get:
            self.assertIsNone(bot.check_exit(position))

        get.assert_called_once_with(
            "DOTUSDT",
            "15m",
            100,
            exchange="binance",
            market_type="futures",
            testnet=True,
            price_type="mark",
        )

    def test_legacy_spot_excursion_is_rebased_once_from_mark_klines(self):
        cfg = TradeConfig(
            mode="paper",
            enabled=False,
            exchange="binance",
            market_type="futures",
            testnet=True,
            enable_time_stop=False,
            use_atr_trail=False,
            tier2_partial_r=2.5,
        )
        bot = SqueezeBreakoutBot(cfg)
        entry_time = datetime(2026, 7, 19, 8, 39, tzinfo=timezone.utc)
        position = Position(
            symbol="DOTUSDT",
            direction="SHORT",
            entry_price=0.832,
            entry_time=entry_time,
            quantity=100.0,
            sl_price=0.84285714,
            current_sl=0.82114286,
            risk_usdt=1.0,
            signal_score=100.0,
            initial_sl=0.84285714,
            initial_band_hi=0.84,
            initial_band_lo=0.83,
            source_interval="30m",
            breakeven_triggered=True,
            partial_tp_triggered=True,
            max_favorable_r=2.76315862,
        )
        rows = 30
        step_ms = 1_800_000
        entry_ms = int(entry_time.timestamp() * 1000)
        first_open_ms = ((entry_ms + step_ms - 1) // step_ms) * step_ms
        frame = pd.DataFrame({
            "ot": [first_open_ms + i * step_ms for i in range(rows)],
            "o": [0.82] * rows,
            "h": [0.825] * rows,
            "l": [0.807] * (rows - 1) + [0.804],
            "c": [0.81] * rows,
            "v": [1.0] * rows,
        })

        with patch("trader.fetch_klines", return_value=frame), patch(
            "trader.fetch_klines_range",
            return_value=frame,
        ):
            bot.check_exit(position)

        expected_mfe = (0.832 - 0.804) / (0.84285714 - 0.832)
        self.assertAlmostEqual(position.max_favorable_r, expected_mfe)
        self.assertEqual(getattr(position, "excursion_price_source", ""), "mark")

    def test_new_position_does_not_use_pre_entry_mark_price_extrema(self):
        cfg = TradeConfig(
            mode="paper",
            enabled=False,
            exchange="binance",
            market_type="futures",
            testnet=True,
            enable_time_stop=False,
            half_risk_trigger_r=0.5,
            enable_early_protect=True,
            early_protect_r=0.8,
            early_protect_lock_r=0.0,
            use_atr_trail=False,
        )
        bot = SqueezeBreakoutBot(cfg)
        real_entry = datetime(2026, 7, 21, 11, 25, tzinfo=timezone.utc)
        display_entry = real_entry + timedelta(hours=8)
        position = Position(
            symbol="NIGHTUSDT",
            direction="LONG",
            entry_price=100.0,
            entry_time=display_entry,
            quantity=10.0,
            sl_price=90.0,
            current_sl=90.0,
            risk_usdt=100.0,
            signal_score=100.0,
            initial_sl=90.0,
            initial_band_hi=101.0,
            initial_band_lo=99.0,
        )
        rows = 30
        first_open = real_entry - timedelta(minutes=30 * rows)
        frame = pd.DataFrame({
            "ot": [int((first_open + timedelta(minutes=30 * i)).timestamp() * 1000) for i in range(rows)],
            "o": [100.0] * rows,
            "h": [112.0] * rows,
            "l": [95.0] * rows,
            "c": [100.1] * rows,
            "v": [1.0] * rows,
        })

        with patch("trader.fetch_klines", return_value=frame):
            reason = bot.check_exit(position)

        self.assertIsNone(reason)
        self.assertFalse(position.breakeven_triggered)
        self.assertFalse(position.half_risk_protected)
        self.assertEqual(position.current_sl, position.initial_sl)
        self.assertEqual(position.excursion_price_source, "")
        self.assertLess(position.max_favorable_r, cfg.early_protect_r)


if __name__ == "__main__":
    unittest.main()
