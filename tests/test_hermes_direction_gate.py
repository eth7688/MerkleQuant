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
    def valid_result(direction):
        return {
            "decision": "neutral" if direction == "NEUTRAL" else "allow",
            "direction": direction,
            "confidence": 1.0,
            "risk_flags": [],
            "skill_used": "kline-indicator",
            "mode_used": "full",
            "data_source": "okx_cli",
            "indicators_checked": ["rsi_14", "macd_12_26_9"],
        }

    def test_allows_same_direction_without_using_confidence(self):
        ok, reason = self.bot._hermes_direction_gate("LONG", self.valid_result("LONG"), False)

        self.assertTrue(ok)
        self.assertEqual(reason, "direction_match")

    def test_blocks_explicit_opposite_direction(self):
        ok, reason = self.bot._hermes_direction_gate("LONG", self.valid_result("SHORT"), False)

        self.assertFalse(ok)
        self.assertEqual(reason, "direction_opposite")

    def test_allows_valid_neutral_as_abstention(self):
        ok, reason = self.bot._hermes_direction_gate("SHORT", self.valid_result("NEUTRAL"), False)

        self.assertTrue(ok)
        self.assertEqual(reason, "neutral_abstain")

    def test_operational_failure_uses_fail_open(self):
        timeout = self.valid_result("NEUTRAL")
        timeout.update({"decision": "timeout", "indicators_checked": []})

        blocked, blocked_reason = self.bot._hermes_direction_gate("LONG", timeout, False)
        allowed, allowed_reason = self.bot._hermes_direction_gate("LONG", timeout, True)

        self.assertFalse(blocked)
        self.assertEqual(blocked_reason, "operational_failure:timeout")
        self.assertTrue(allowed)
        self.assertEqual(allowed_reason, "operational_failure:timeout:fail_open")

    def test_data_unavailable_is_not_valid_neutral(self):
        unavailable = self.valid_result("NEUTRAL")
        unavailable.update({"risk_flags": ["data_unavailable"], "indicators_checked": []})

        ok, reason = self.bot._hermes_direction_gate("LONG", unavailable, False)

        self.assertFalse(ok)
        self.assertEqual(reason, "operational_failure:data_unavailable")


if __name__ == "__main__":
    unittest.main()
