import math
import unittest
from datetime import datetime, timezone

from performance_metrics import build_r_trade_lifecycles, summarize_r_performance_ranges


class RLifecycleTests(unittest.TestCase):
    def test_groups_partial_exits_by_signal_key_and_sums_actual_r(self):
        rows = [
            {
                "time": "2026-07-27T17:29:16+00:00",
                "signal_key": "PREDICTA|DIA|LONG|30m|1",
                "direction": "LONG",
                "risk": 74,
                "pnl": 234.12,
                "r": 3.1639,
                "mfe_r": 6.3385,
                "mae_r": 0.5956,
                "reason": "二阶减仓50%",
            },
            {
                "time": "2026-07-27T20:44:24+00:00",
                "signal_key": "PREDICTA|DIA|LONG|30m|1",
                "direction": "LONG",
                "risk": 74,
                "pnl": 107.17,
                "r": 1.4482,
                "mfe_r": 6.7555,
                "mae_r": 0.5956,
                "reason": "击穿动态追踪防线",
            },
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(result["source_record_count"], 2)
        self.assertEqual(result["valid_exit_record_count"], 2)
        self.assertEqual(result["excluded_records"], 0)
        self.assertEqual(len(result["lifecycles"]), 1)
        trade = result["lifecycles"][0]
        self.assertAlmostEqual(trade["r"], 4.6121, places=4)
        self.assertEqual(trade["mfe_r"], 6.7555)
        self.assertEqual(trade["mae_r"], 0.5956)
        self.assertEqual(trade["final_reason"], "击穿动态追踪防线")
        self.assertEqual(trade["reason_r"]["二阶减仓50%"], 3.1639)
        self.assertEqual(trade["reason_r"]["击穿动态追踪防线"], 1.4482)

    def test_legacy_records_without_signal_key_remain_independent(self):
        rows = [
            {"time": "2026-07-20T10:00:00+00:00", "risk": 100, "pnl": 50, "direction": "LONG"},
            {"time": "2026-07-20T10:05:00+00:00", "risk": 100, "pnl": 25, "direction": "LONG"},
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(len(result["lifecycles"]), 2)
        self.assertEqual([x["r"] for x in result["lifecycles"]], [0.5, 0.25])

    def test_excludes_voided_invalid_risk_time_and_non_finite_r(self):
        rows = [
            {"time": "2026-07-20T10:00:00+00:00", "risk": 100, "r": 1},
            {"time": "2026-07-20T10:01:00+00:00", "risk": 0, "r": 1},
            {"time": "bad-time", "risk": 100, "r": 1},
            {"time": "2026-07-20T10:03:00+00:00", "risk": 100, "r": math.inf},
            {"time": "2026-07-20T10:04:00+00:00", "risk": 100, "r": 1, "voided": True},
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(result["valid_exit_record_count"], 1)
        self.assertEqual(result["excluded_records"], 4)
        self.assertEqual(len(result["lifecycles"]), 1)

    def test_excludes_non_finite_persisted_r_even_when_pnl_can_derive_r(self):
        rows = [
            {
                "time": "2026-07-20T10:00:00+00:00",
                "risk": 100,
                "pnl": 50,
                "r": math.inf,
            }
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(result["valid_exit_record_count"], 0)
        self.assertEqual(result["excluded_records"], 1)
        self.assertEqual(result["lifecycles"], [])

    def test_excludes_records_with_voided_status_case_insensitively(self):
        rows = [
            {
                "time": "2026-07-20T10:00:00+00:00",
                "risk": 100,
                "r": 1,
                "status": "VoIdEd",
            }
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual(result["valid_exit_record_count"], 0)
        self.assertEqual(result["excluded_records"], 1)
        self.assertEqual(result["lifecycles"], [])

    def test_preserves_whitespace_in_signal_key_when_grouping(self):
        rows = [
            {
                "time": "2026-07-20T10:00:00+00:00",
                "signal_key": "key",
                "risk": 100,
                "r": 1,
            },
            {
                "time": "2026-07-20T10:01:00+00:00",
                "signal_key": " key ",
                "risk": 100,
                "r": 2,
            },
        ]

        result = build_r_trade_lifecycles(rows)

        self.assertEqual([trade["key"] for trade in result["lifecycles"]], ["key", " key "])
        self.assertEqual([trade["r"] for trade in result["lifecycles"]], [1.0, 2.0])


class RSummaryTests(unittest.TestCase):
    NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def test_calculates_core_metrics_drawdown_streak_and_breakdowns(self):
        rows = [
            {"time": "2026-07-20T10:00:00+00:00", "risk": 100, "r": 2.0, "mfe_r": 3.0, "direction": "LONG", "reason": "ATR"},
            {"time": "2026-07-21T10:00:00+00:00", "risk": 100, "r": -1.0, "mfe_r": 0.2, "direction": "SHORT", "reason": "SL"},
            {"time": "2026-07-22T10:00:00+00:00", "risk": 100, "r": -0.5, "mfe_r": 0.4, "direction": "SHORT", "reason": "SL"},
            {"time": "2026-07-23T10:00:00+00:00", "risk": 100, "r": 1.0, "mfe_r": 2.0, "direction": "LONG", "reason": "结构退出"},
            {"time": "2026-07-24T10:00:00+00:00", "risk": 100, "r": 0.0, "mfe_r": 0.8, "direction": "LONG", "reason": "保本"},
        ]

        summary = summarize_r_performance_ranges(rows, self.NOW)["ranges"]["all"]

        self.assertEqual(summary["valid_trade_count"], 5)
        self.assertAlmostEqual(summary["net_r"], 1.5)
        self.assertAlmostEqual(summary["expectancy_r"], 0.3)
        self.assertAlmostEqual(summary["win_rate"], 40.0)
        self.assertAlmostEqual(summary["average_win_r"], 1.5)
        self.assertAlmostEqual(summary["average_loss_r"], -0.75)
        self.assertAlmostEqual(summary["average_payoff_ratio"], 2.0)
        self.assertAlmostEqual(summary["profit_factor"], 2.0)
        self.assertAlmostEqual(summary["max_drawdown_r"], 1.5)
        self.assertEqual(summary["max_consecutive_losses"], 2)
        self.assertEqual(summary["largest_win_r"], 2.0)
        self.assertEqual(summary["largest_loss_r"], -1.0)
        self.assertEqual(summary["direction_breakdown"]["LONG"]["net_r"], 3.0)
        self.assertEqual(summary["direction_breakdown"]["SHORT"]["net_r"], -1.5)
        self.assertEqual(summary["exit_reason_breakdown"]["SL"]["net_r"], -1.5)
        self.assertAlmostEqual(summary["average_mfe_r"], 1.28)
        self.assertAlmostEqual(summary["mfe_capture_efficiency"], 0.6)

    def test_groups_before_filtering_and_uses_final_exit_time_for_range(self):
        key = "PREDICTA|PARTIAL|LONG|30m|1"
        rows = [
            {"time": "2026-07-01T10:00:00+00:00", "signal_key": key, "risk": 100, "r": 1.0},
            {"time": "2026-07-28T10:00:00+00:00", "signal_key": key, "risk": 100, "r": 0.5},
            {"time": "2026-07-10T10:00:00+00:00", "risk": 100, "r": -1.0},
        ]

        result = summarize_r_performance_ranges(rows, self.NOW)

        self.assertEqual(result["ranges"]["7"]["valid_trade_count"], 1)
        self.assertAlmostEqual(result["ranges"]["7"]["net_r"], 1.5)
        self.assertEqual(result["ranges"]["30"]["valid_trade_count"], 2)

    def test_returns_none_for_undefined_ratios(self):
        rows = [
            {"time": "2026-07-28T10:00:00+00:00", "risk": 100, "r": 1.0},
        ]

        summary = summarize_r_performance_ranges(rows, self.NOW)["ranges"]["all"]

        self.assertIsNone(summary["average_loss_r"])
        self.assertIsNone(summary["average_payoff_ratio"])
        self.assertIsNone(summary["profit_factor"])
