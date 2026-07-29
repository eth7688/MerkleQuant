import unittest
from pathlib import Path
from unittest.mock import patch

from trader import SqueezeBreakoutBot


class RPerformanceIntegrationTests(unittest.TestCase):
    def test_engine_caches_r_summary_for_fast_polling(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.trade_log = [{"time": "2026-07-29T10:00:00+00:00", "risk": 100, "r": 1}]
        bot._fast_r_performance_cache_ts = 0.0
        bot._fast_r_performance_cache = {}
        bot._log_ready = False

        expected = {"status": "ok", "ranges": {"all": {"net_r": 1}}}
        with patch("trader.summarize_r_performance_ranges", return_value=expected) as calculate:
            first = bot._r_performance_summary()
            second = bot._r_performance_summary()

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        calculate.assert_called_once()

    def test_both_status_methods_publish_the_same_named_payload(self):
        source = Path("trader.py").read_text(encoding="utf-8")

        self.assertEqual(source.count('"r_performance": r_performance'), 2)
        self.assertGreaterEqual(source.count("r_performance = self._r_performance_summary()"), 2)
