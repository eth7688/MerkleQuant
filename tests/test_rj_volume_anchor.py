import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import SqueezeBreakoutBot, TradeConfig


class RjVolumeAnchorTest(unittest.TestCase):
    def test_setup_pool_uses_signal_key_volume_not_live_confirmation_volume(self):
        cfg = TradeConfig(
            mode="paper",
            enabled=False,
            exchange="bitget",
            entry_signal_source="rj_only",
            max_positions=3,
            rj_only_setup_check_interval_sec=60,
            rj_only_setup_max_chase_pct=2.0,
            rj_only_volume_filter=True,
        )
        bot = SqueezeBreakoutBot(cfg)
        now = time.time()
        setup_key = "RJSETUP|TESTUSDT|LONG|30m|1000|0.99000000"
        bot._rj_setup_pool = {
            setup_key: {
                "symbol": "TESTUSDT",
                "direction": "LONG",
                "source_interval": "30m",
                "score": 80.0,
                "source_strategy": "rj_only",
                "rj_setup_key": setup_key,
                "rj_setup_first_seen_ts": now - 30,
                "rj_setup_expires_at_ts": now + 3600,
                "rj_setup_trigger_price": 1.0,
                "rj_only_key_high": 0.99,
                "rj_only_key_low": 0.95,
                "rj_only_atr": 0.02,
                "rj_volume_filter_pass": True,
                "rj_volume_reason": "pass",
                "rj_volume_ratio": 2.0,
                "rj_volume_bar": 123,
                "rj_volume_anchor": "signal_key",
            }
        }

        entered = []
        events = []
        bot._last_rj_setup_check_ts = 0.0
        bot._is_live_trade_symbol = lambda symbol: True
        bot._btc_regime_fields = lambda: {"btc_regime": "test", "btc_score": 0}
        bot._get_rj_setup_closed_kline = lambda symbol, interval: {"close": 1.01}
        bot._rj_only_live_volume_state = lambda symbol, interval: {
            "rj_volume_filter_pass": False,
            "rj_volume_reason": "volume_ratio_low",
            "rj_volume_ratio": 0.05,
            "rj_volume_checked_live": True,
            "rj_volume_anchor": "confirm_live",
        }
        bot._append_signal_event = lambda event, symbol=None, payload=None: events.append((event, symbol, payload or {}))
        bot.enter_rj_position = lambda signal: entered.append(signal) or object()

        bot._check_rj_setup_pool()

        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0]["rj_volume_anchor"], "signal_key")
        self.assertNotIn("rj_setup_volume_wait", [event for event, _, _ in events])
        self.assertNotIn("entry_reject", [event for event, _, _ in events])


if __name__ == "__main__":
    unittest.main()
