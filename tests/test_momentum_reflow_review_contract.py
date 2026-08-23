import unittest
from pathlib import Path


class MomentumReflowReviewContractTests(unittest.TestCase):
    def test_implementation_plan_uses_approved_thousand_bar_context(self):
        plan = Path("docs/superpowers/plans/2026-07-30-momentum-reflow-scanner.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("1000", plan)
        self.assertNotIn("60 * 3_600_000", plan)


if __name__ == "__main__":
    unittest.main()
