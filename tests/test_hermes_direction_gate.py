import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import SqueezeBreakoutBot, TradeConfig


class HermesDirectionGateTest(unittest.TestCase):
    def setUp(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget", entry_signal_source="rj_only")
        self.bot = SqueezeBreakoutBot(cfg)

    @staticmethod
    def valid_result(direction, tradeable=True):
        return {
            "analysis_status": "OK",
            "dominant_direction": direction,
            "tradeable": tradeable,
            "market_regime": "TREND" if tradeable else "CONFLICT",
            "confidence": 1.0,
            "risk_flags": [],
            "skill_used": "kline-indicator",
            "mode_used": "full",
            "data_source": "okx_cli",
            "pillars_checked": ["macro_cycle", "price_volume_factors", "derivatives"],
            "indicators_checked": ["rsi_14", "macd_12_26_9"],
            "evidence": {
                "macro_cycle": "LONG",
                "price_volume_factors": "LONG",
                "derivatives": "LONG",
            },
        }

    def test_allows_same_direction_without_using_confidence(self):
        ok, reason = self.bot._hermes_direction_gate("LONG", self.valid_result("LONG"), False)

        self.assertTrue(ok)
        self.assertEqual(reason, "direction_match")

    def test_blocks_explicit_opposite_direction(self):
        ok, reason = self.bot._hermes_direction_gate("LONG", self.valid_result("SHORT"), False)

        self.assertFalse(ok)
        self.assertEqual(reason, "direction_opposite")

    def test_blocks_valid_analysis_when_market_is_not_tradeable(self):
        ok, reason = self.bot._hermes_direction_gate(
            "SHORT", self.valid_result("SHORT", tradeable=False), False
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "market_not_tradeable")

    def test_rejects_neutral_direction_after_successful_analysis(self):
        ok, reason = self.bot._hermes_direction_gate("SHORT", self.valid_result("NEUTRAL"), False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:invalid_direction")

    def test_rejects_analysis_without_three_completed_pillars(self):
        incomplete = self.valid_result("LONG")
        incomplete.update({"pillars_checked": ["trend"], "evidence": {"trend": "LONG"}})

        ok, reason = self.bot._hermes_direction_gate("LONG", incomplete, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:pillars_incomplete")

    def test_rejects_malformed_pillar_evidence(self):
        malformed = self.valid_result("LONG")
        malformed.update({"pillars_checked": "trend,momentum,volume", "evidence": ["a", "b", "c"]})

        ok, reason = self.bot._hermes_direction_gate("LONG", malformed, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:pillars_incomplete")

    def test_rejects_fake_three_pillars(self):
        fake = self.valid_result("LONG")
        fake.update({"pillars_checked": ["a", "b", "c"], "evidence": {"a": 1, "b": 2, "c": 3}})

        ok, reason = self.bot._hermes_direction_gate("LONG", fake, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:pillars_incomplete")

    def test_rejects_missing_analysis_status(self):
        missing = self.valid_result("LONG")
        missing.pop("analysis_status")

        ok, reason = self.bot._hermes_direction_gate("LONG", missing, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:status_missing")

    def test_rejects_missing_tradeable(self):
        missing = self.valid_result("LONG")
        missing.pop("tradeable")

        ok, reason = self.bot._hermes_direction_gate("LONG", missing, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:tradeable_missing")

    def test_nonzero_hermes_process_exit_is_operational_failure(self):
        failed = self.valid_result("LONG")
        failed["_process_returncode"] = 1

        ok, reason = self.bot._hermes_direction_gate("LONG", failed, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:process_error")

    def test_operational_failure_uses_fail_open(self):
        timeout = self.valid_result("LONG")
        timeout.update({"analysis_status": "TIMEOUT", "indicators_checked": []})

        blocked, blocked_reason = self.bot._hermes_direction_gate("LONG", timeout, False)
        allowed, allowed_reason = self.bot._hermes_direction_gate("LONG", timeout, True)

        self.assertFalse(blocked)
        self.assertEqual(blocked_reason, "operational_failure:timeout")
        self.assertTrue(allowed)
        self.assertEqual(allowed_reason, "operational_failure:timeout:fail_open")

    def test_data_unavailable_is_not_valid_neutral(self):
        unavailable = self.valid_result("LONG")
        unavailable.update({"analysis_status": "NO_DATA", "indicators_checked": []})

        ok, reason = self.bot._hermes_direction_gate("LONG", unavailable, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:data_unavailable")

    def test_prompt_starts_with_full_analysis_skill_trigger(self):
        prompt = self.bot._build_hermes_direction_prompt("BTCUSDT", "30m")

        self.assertTrue(prompt.startswith("BTC 完整分析\n"))
        self.assertIn("三大支柱", prompt)
        self.assertIn('"dominant_direction":"LONG|SHORT"', prompt)
        self.assertNotIn("AXIOM计划方向", prompt)

    def test_public_result_keeps_full_analysis_evidence(self):
        state = self.valid_result("LONG", tradeable=False)
        state.update({"active": True, "pass": False})

        public = self.bot._public_hermes_confirm(state)

        self.assertEqual(public["analysis_status"], "OK")
        self.assertEqual(public["dominant_direction"], "LONG")
        self.assertFalse(public["tradeable"])
        self.assertEqual(len(public["pillars_checked"]), 3)
        self.assertEqual(public["evidence"]["macro_cycle"], "LONG")

    def test_public_result_normalizes_direction_and_status_whitespace(self):
        state = self.valid_result("LONG")
        state.update({"analysis_status": " OK ", "dominant_direction": " LONG ", "direction": " LONG "})

        public = self.bot._public_hermes_confirm(state)

        self.assertEqual(public["analysis_status"], "OK")
        self.assertEqual(public["dominant_direction"], "LONG")
        self.assertEqual(public["direction"], "LONG")


if __name__ == "__main__":
    unittest.main()
