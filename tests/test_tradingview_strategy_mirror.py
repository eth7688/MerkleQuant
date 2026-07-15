import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINE = ROOT / "tradingview_rj_strategy_mirror.pine"


class TradingViewStrategyMirrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PINE.read_text(encoding="utf-8")

    def test_is_strategy_with_next_bar_execution(self):
        self.assertIn("strategy(", self.source)
        self.assertNotIn("indicator(", self.source)
        self.assertIn("process_orders_on_close=false", self.source)
        self.assertIn("calc_on_order_fills=false", self.source)
        self.assertIn("pyramiding=0", self.source)

    def test_rejects_non_30_minute_charts(self):
        self.assertIn("isThirtyMinute", self.source)
        self.assertIn('runtime.error("本策略仅支持30分钟图表")', self.source)

    def test_contains_signal_key_choppy_filter(self):
        self.assertIn('choppyMode = input.string("硬过滤"', self.source)
        self.assertIn("keyChoppy", self.source)
        self.assertIn("atrContraction", self.source)
        self.assertIn("boxSqueeze", self.source)
        self.assertIn("middleChop", self.source)

    def test_contains_causal_recent_win_rate_gate(self):
        self.assertIn("statsLookbackBars", self.source)
        self.assertIn("statsHorizonBars", self.source)
        self.assertIn("longStatsPass", self.source)
        self.assertIn("shortStatsPass", self.source)
        self.assertIn("pendingDirections", self.source)

    def test_recent_stats_are_not_preselected_by_entry_filters(self):
        self.assertIn("setupKeyFilterOk", self.source)
        self.assertIn("longRawAccepted = rawLongKey and setupDir == 0", self.source)
        self.assertIn("shortRawAccepted = rawShortKey and setupDir == 0", self.source)
        self.assertIn("entryAllowed = riskValid and setupKeyFilterOk", self.source)
        self.assertIn("if riskValid\n            newSampleTarget", self.source)

    def test_na_typed_order_values_are_explicit(self):
        self.assertIn("float plannedStop = na", self.source)

    def test_status_table_separates_long_and_short_recent_stats(self):
        self.assertIn('"做多近期"', self.source)
        self.assertIn('"做空近期"', self.source)
        self.assertIn("f_stats_text(longSamples, longWinRate, longAvgR)", self.source)
        self.assertIn("f_stats_text(shortSamples, shortWinRate, shortAvgR)", self.source)

    def test_contains_strategy_entries_and_demo_exit_profile(self):
        self.assertIn('strategy.entry("RJ-L"', self.source)
        self.assertIn('strategy.entry("RJ-S"', self.source)
        self.assertIn("earlyProtectR", self.source)
        self.assertIn("tier1R", self.source)
        self.assertIn("tier2R", self.source)
        self.assertIn("chandelierMult", self.source)

    def test_has_no_explicit_lookahead_or_future_plot_offset(self):
        self.assertNotIn("lookahead_on", self.source)
        self.assertNotRegex(self.source, r"offset\s*=\s*-\d")

    def test_time_inputs_use_const_unix_milliseconds(self):
        self.assertIn("startTime = input.time(1704067200000", self.source)
        self.assertIn("endTime = input.time(1924991940000", self.source)
        self.assertNotRegex(self.source, r"input\.time\(timestamp\(")


if __name__ == "__main__":
    unittest.main()
