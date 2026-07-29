import logging
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from trader import BinanceClient, SqueezeBreakoutBot, TradeConfig


class _FuturesClient:
    def __init__(self, leverage_result=None, lot_max="1000000", market_max="500"):
        self.leverage_result = leverage_result
        self.filters = [
            {
                "filterType": "LOT_SIZE",
                "minQty": "1",
                "maxQty": lot_max,
                "stepSize": "1",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "1",
                "maxQty": market_max,
                "stepSize": "1",
            },
        ]

    def get_symbol_info(self, _symbol):
        return {"status": "TRADING", "filters": self.filters}

    def set_compatible_leverage(self, _symbol, _leverage):
        return self.leverage_result


class _OrderClient(_FuturesClient):
    def __init__(self, leverage_result=None):
        super().__init__(leverage_result=leverage_result, market_max="1000000")
        self.market_order_calls = []

    def set_leverage(self, _symbol, _leverage):
        return None

    def market_order(self, symbol, side, quantity):
        self.market_order_calls.append((symbol, side, quantity))
        return {"orderId": 1}

    def stop_order(self, *_args, **_kwargs):
        return None


def _frame(size=160):
    close = np.linspace(100.0, 110.0, size)
    return pd.DataFrame({
        "ot": np.arange(size, dtype=np.int64) * 1_800_000,
        "o": close - 0.1,
        "h": close + 0.5,
        "l": close - 0.5,
        "c": close,
        "v": np.full(size, 1000.0),
    })


def _bot(client):
    bot = object.__new__(SqueezeBreakoutBot)
    bot.cfg = TradeConfig(
        exchange="binance",
        market_type="futures",
        leverage=25,
    )
    bot.client = client
    bot._log = logging.getLogger("entry-order-compatibility-test")
    return bot


class EntryOrderCompatibilityTest(unittest.TestCase):
    def test_floor_qty_prefers_market_lot_size_for_market_orders(self):
        bot = _bot(_FuturesClient())

        quantity = bot._floor_qty("KAITOUSDT", 3089.0)

        self.assertEqual(quantity, 500.0)

    def test_binance_leverage_falls_back_from_25_to_20(self):
        client = BinanceClient(market_type="futures")
        response = {"leverage": 20, "maxNotionalValue": "2500"}

        with patch.object(client, "set_leverage", side_effect=[None, response]) as setter:
            result = client.set_compatible_leverage("ZAMAUSDT", 25)

        self.assertEqual([call.args[1] for call in setter.call_args_list], [25, 20])
        self.assertEqual(result, response)

    def test_prepare_futures_entry_caps_notional_and_recomputes_risk(self):
        client = _FuturesClient(
            leverage_result={"leverage": 20, "maxNotionalValue": "2500"},
            market_max="1000000",
        )
        bot = _bot(client)

        qty, notional, risk, meta = bot._prepare_futures_entry(
            "ZAMAUSDT", entry_price=10.0, sl_price=9.0, qty=300.0
        )

        self.assertEqual(qty, 245.0)
        self.assertEqual(notional, 2450.0)
        self.assertEqual(risk, 245.0)
        self.assertEqual(meta["requested_leverage"], 25)
        self.assertEqual(meta["effective_leverage"], 20)
        self.assertEqual(meta["max_notional_value"], 2500.0)

    def test_prepare_futures_entry_blocks_when_no_leverage_is_supported(self):
        bot = _bot(_FuturesClient(leverage_result=None))

        qty, notional, risk, meta = bot._prepare_futures_entry(
            "ZAMAUSDT", entry_price=10.0, sl_price=9.0, qty=300.0
        )

        self.assertEqual((qty, notional, risk), (0.0, 0.0, 0.0))
        self.assertEqual(meta["reason"], "leverage_unavailable")

    def test_predicta_entry_does_not_order_when_leverage_is_unavailable(self):
        bot = SqueezeBreakoutBot(TradeConfig(
            mode="paper",
            exchange="binance",
            market_type="futures",
            entry_signal_source="predicta_ewo",
            leverage=25,
        ))
        client = _OrderClient(leverage_result=None)
        bot.client = client
        bot.cfg.mode = "live"
        events = []
        signal = {
            "symbol": "ZAMAUSDT",
            "direction": "LONG",
            "price": 110.0,
            "score": 100.0,
            "source_interval": "30m",
            "source_strategy": "predicta_ewo",
            "signal_key": "PREDICTA|ZAMAUSDT|LONG|30m|1",
            "predicta_stop_price": 105.0,
            "predicta_entry_path": "fast",
            "choppy_filter_mode": "hard",
            "choppy_filter_is_choppy": False,
        }

        def record_event(event, symbol, payload):
            events.append((event, symbol, payload))

        with patch("trader.fetch_klines", return_value=_frame()), patch.object(
            bot, "_is_live_trade_symbol", return_value=True
        ), patch.object(bot, "_save_positions"), patch.object(
            bot, "_append_signal_event", side_effect=record_event
        ):
            position = bot.enter_predicta_position(signal)

        self.assertIsNone(position)
        self.assertEqual(client.market_order_calls, [])
        self.assertIn(
            "leverage_unavailable",
            [payload.get("reason") for event, _, payload in events if event == "entry_reject"],
        )


if __name__ == "__main__":
    unittest.main()
