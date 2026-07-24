import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import Position, SqueezeBreakoutBot, TradeConfig


class FakeFuturesClient:
    def __init__(self):
        self._active_stop_ids = {}

    def get_positions(self):
        return [
            {
                "symbol": "LTCUSDT",
                "positionAmt": 2.0,
                "entryPrice": 101.25,
                "markPrice": 105.5,
                "unRealizedProfit": 8.5,
                "openTime": "",
            }
        ]


class ExchangeEntryPriceSyncTest(unittest.TestCase):
    def test_existing_position_entry_price_is_overwritten_by_exchange_average(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget", market_type="futures")
        bot = SqueezeBreakoutBot(cfg)
        bot.client = FakeFuturesClient()
        old_pos = Position(
            symbol="LTCUSDT",
            direction="LONG",
            entry_price=100.0,
            entry_time=datetime.fromisoformat("2026-07-09T20:00:00+08:00"),
            quantity=2.0,
            sl_price=95.0,
            current_sl=95.0,
            risk_usdt=10.0,
            signal_score=80.0,
            initial_band_hi=102.0,
            initial_band_lo=98.0,
            target_zone_price=110.0,
            target_r=2.0,
        )
        bot.positions = [old_pos]

        with tempfile.TemporaryDirectory() as tmp:
            bot._positions_path = str(Path(tmp) / "positions.json")
            with patch("trader.fetch_klines", return_value=None):
                bot._sync_positions()

        self.assertEqual(len(bot.positions), 1)
        self.assertEqual(bot.positions[0].entry_price, 101.25)
        self.assertEqual(bot.positions[0].current_price, 105.5)
        self.assertEqual(bot.positions[0].pnl, 8.5)
        self.assertEqual(bot.positions[0].quantity, 2.0)
        self.assertEqual(bot.positions[0].target_r, 1.4)

    def test_fast_summary_refreshes_entry_price_from_exchange_snapshot(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget", market_type="futures")
        bot = SqueezeBreakoutBot(cfg)
        bot.client = FakeFuturesClient()
        bot.positions = [
            Position(
                symbol="LTCUSDT",
                direction="LONG",
                entry_price=100.0,
                entry_time=datetime.fromisoformat("2026-07-09T20:00:00+08:00"),
                quantity=2.0,
                sl_price=95.0,
                current_sl=95.0,
                risk_usdt=10.0,
                signal_score=80.0,
                initial_band_hi=102.0,
                initial_band_lo=98.0,
                target_zone_price=110.0,
                target_r=2.0,
            )
        ]

        summary = bot.get_fast_summary()

        self.assertEqual(summary["positions"][0]["entry"], 101.25)
        self.assertEqual(summary["positions"][0]["current_price"], 105.5)
        self.assertEqual(summary["positions"][0]["pnl"], 8.5)
        self.assertEqual(summary["positions"][0]["target_r"], 1.4)


if __name__ == "__main__":
    unittest.main()
