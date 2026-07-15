import unittest
from pathlib import Path

from trader import SqueezeBreakoutBot, TradeConfig


ROOT = Path(__file__).resolve().parents[1]


class PredictaConfigTest(unittest.TestCase):
    def test_predicta_defaults_match_approved_strategy(self):
        cfg = TradeConfig()

        self.assertEqual(cfg.predicta_confirm_bars, 6)
        self.assertEqual(cfg.predicta_choppy_filter_mode, "hard")
        self.assertEqual(cfg.predicta_ewo_fast, 5)
        self.assertEqual(cfg.predicta_ewo_slow, 35)
        self.assertEqual(cfg.predicta_confirm_atr_buffer, 0.08)
        self.assertEqual(cfg.predicta_stop_atr_mult, 0.5)
        self.assertEqual(cfg.predicta_min_stop_pct, 0.003)
        self.assertEqual(cfg.predicta_max_stop_pct, 0.08)
        self.assertEqual(cfg.predicta_max_symbols, 500)
        self.assertEqual(cfg.predicta_scan_workers, 4)

    def test_signal_source_normalizes_predicta_aliases(self):
        bot = object.__new__(SqueezeBreakoutBot)
        for value in ("predicta_ewo", "predicta-ewo", "predicta"):
            bot.cfg = TradeConfig(entry_signal_source=value)
            self.assertEqual(bot._entry_signal_source(), "predicta_ewo")

    def test_admin_and_user_config_offer_predicta_source(self):
        for name in ("admin_server.py", "web_ui.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('option value="predicta_ewo"', source)


if __name__ == "__main__":
    unittest.main()
