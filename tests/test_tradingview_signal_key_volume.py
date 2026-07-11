import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINE = ROOT / "tradingview_rj_bbkd_indicator.pine"


class TradingViewSignalKeyVolumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PINE.read_text(encoding="utf-8")

    def test_confirmation_does_not_recheck_current_candle_volume(self):
        long_line = re.search(r"^\s*longConfirmOk\s*=.*$", self.source, re.MULTILINE).group(0)
        short_line = re.search(r"^\s*shortConfirmOk\s*=.*$", self.source, re.MULTILINE).group(0)

        for line in (long_line, short_line):
            self.assertIn("BoxFilterOk", line)
            self.assertNotIn("volumeOk", line)
            self.assertNotIn("adxOk", line)
            self.assertNotIn("DirectionOk", line)

    def test_engine_alignment_uses_key_candle_volume(self):
        self.assertIn("engineLongVolumeRatioKey = volumeRatio[i]", self.source)
        self.assertIn("engineShortVolumeRatioKey = volumeRatio[i]", self.source)
        self.assertIn(
            "engineLongFilterOk = engineLongDirectionOkKey and engineLongAdxOkKey and engineLongVolumeOkKey and cooldownOk",
            self.source,
        )
        self.assertIn(
            "engineShortFilterOk = engineShortDirectionOkKey and engineShortAdxOkKey and engineShortVolumeOkKey and cooldownOk",
            self.source,
        )
        self.assertNotIn("engineLongFilterOk = longDirectionOk and adxOk and volumeOk", self.source)
        self.assertNotIn("engineShortFilterOk = shortDirectionOk and adxOk and volumeOk", self.source)

    def test_data_window_reports_signal_key_volume(self):
        self.assertIn("activeSignalVolumeRatio", self.source)
        self.assertIn("信号K成交量过滤通过", self.source)
        self.assertIn("信号K成交量/均量倍数", self.source)


if __name__ == "__main__":
    unittest.main()
