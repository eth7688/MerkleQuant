import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader import SqueezeBreakoutBot, TradeConfig


class TargetZoneSelectionTest(unittest.TestCase):
    def setUp(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget", entry_signal_source="rj_only")
        self.bot = SqueezeBreakoutBot(cfg)

    def test_selects_narrowest_forward_squeeze_instead_of_nearest_swing(self):
        count = 200
        df = pd.DataFrame({
            "h": np.full(count, 100.2),
            "l": np.full(count, 99.8),
            "c": np.full(count, 100.0),
        })
        df.loc[150, "h"] = 101.0

        min_band = np.full(count, 99.5)
        max_band = np.full(count, 100.5)
        spread = np.full(count, 2.0)
        min_band[130:140] = 104.0
        max_band[130:140] = 105.0
        spread[130:140] = 0.5
        min_band[160:170] = 110.0
        max_band[160:170] = 110.2
        spread[160:170] = 0.1

        with patch("trader.calc_ma_band", return_value=(max_band, min_band, spread)), patch(
            "trader.get_squeeze_max", return_value=1.0
        ):
            result = self.bot._calc_target_zone(
                df, "LONG", entry_price=100.0, sl_price=98.0, interval="30m", signal={}
            )

        self.assertEqual(result["target_zone_type"], "squeeze_resistance")
        self.assertEqual(result["target_zone_price"], 110.0)
        self.assertEqual(result["target_r"], 5.0)

    def test_recalculates_target_r_from_exchange_entry_price(self):
        pos = SimpleNamespace(
            direction="LONG",
            entry_price=101.0,
            initial_sl=99.0,
            sl_price=99.0,
            current_sl=99.0,
            target_zone_price=105.0,
            target_r=1.0,
            target_distance_pct=1.0,
        )

        self.bot._refresh_target_metrics(pos)

        self.assertEqual(pos.target_r, 2.0)
        self.assertAlmostEqual(pos.target_distance_pct, 105.0 / 101.0 * 100 - 100, places=4)

    def test_hides_target_that_is_behind_exchange_entry(self):
        pos = SimpleNamespace(
            direction="LONG",
            entry_price=101.0,
            initial_sl=99.0,
            sl_price=99.0,
            current_sl=99.0,
            target_zone_price=100.5,
            target_r=1.0,
            target_distance_pct=1.0,
        )

        self.bot._refresh_target_metrics(pos)

        self.assertEqual(pos.target_r, 0.0)
        self.assertEqual(pos.target_distance_pct, 0.0)

    def test_clears_legacy_swing_target_from_open_position(self):
        pos = SimpleNamespace(
            direction="LONG",
            entry_price=100.0,
            initial_sl=98.0,
            sl_price=98.0,
            current_sl=98.0,
            target_zone_type="swing_resistance",
            target_zone_price=101.0,
            target_zone_low=101.0,
            target_zone_high=101.0,
            target_zone_bars_ago=10,
            target_r=0.5,
            target_distance_pct=1.0,
        )

        self.bot._refresh_target_metrics(pos)

        self.assertEqual(pos.target_zone_type, "none")
        self.assertEqual(pos.target_zone_price, 0.0)
        self.assertEqual(pos.target_r, 0.0)


if __name__ == "__main__":
    unittest.main()
