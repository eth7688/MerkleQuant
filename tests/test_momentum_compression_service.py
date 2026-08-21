import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd

from momentum_compression_store import default_state, load_compression_state, save_compression_state


BASE_TIME = 1_700_000_000_000


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def fake_binance_get(quote_volumes):
    exchange = {
        "symbols": [
            {"symbol": "KEEPUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "DROPUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "DELIVERYUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT"},
            {"symbol": "PAUSEDUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BTCBUSD", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "BUSD"},
            {"symbol": "EURUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        ]
    }
    tickers = [{"symbol": symbol, "quoteVolume": str(volume)} for symbol, volume in quote_volumes.items()]

    def get(url, timeout):
        return FakeResponse(exchange if url.endswith("exchangeInfo") else tickers)

    return get


def trend_frame(side, bars=30):
    index = np.arange(bars, dtype=float)
    center = 100 + (0.2 * index if side == "LONG" else -0.2 * index)
    wave = 2.0 * np.sin(index * np.pi / 3)
    return pd.DataFrame({
        "ot": BASE_TIME + index.astype(int) * 3_600_000,
        "o": center - 0.2,
        "h": center + 1.0 + wave,
        "l": center - 1.0 + wave,
        "c": center + (0.3 if side == "LONG" else -0.3),
        "v": np.full(bars, 1000.0),
    })


def pool_item(symbol="KEEPUSDT"):
    return {
        "symbol": symbol, "side": "LONG", "compression_id": "keep-long",
        "state": "PRE_BREAKOUT", "fresh_emitted": False,
        "upper_boundary_price": 110.0, "lower_boundary_price": 100.0,
        "breakout_buffer_price": 0.5, "first_seen_at": 1,
        "last_verified_at": 1,
    }


def eligible_evaluation(symbol="KEEPUSDT"):
    return {
        **pool_item(symbol), "evaluated_at": BASE_TIME + 900_000,
        "htf_alignment": "UNKNOWN", "parameter_version": "15m-compression-v1",
        "compression_start_time": BASE_TIME, "compression_end_time": BASE_TIME,
        "atr14": 1.0, "compression_bars": 20, "rejection_reasons": [],
        "directional_touch_times": [],
    }


class CompressionUniverseTests(unittest.TestCase):
    def test_universe_uses_one_million_quote_volume_floor_and_perpetual_filters(self):
        from momentum_compression_service import fetch_compression_universe

        symbols, volumes = fetch_compression_universe(get=fake_binance_get({
            "KEEPUSDT": 1_000_000,
            "DROPUSDT": 999_999.99,
            "DELIVERYUSDT": 9_000_000,
            "PAUSEDUSDT": 9_000_000,
            "BTCBUSD": 9_000_000,
            "EURUSDT": 9_000_000,
        }))

        self.assertEqual(symbols, ["KEEPUSDT"])
        self.assertEqual(volumes["KEEPUSDT"], 1_000_000.0)


class HtfAlignmentTests(unittest.TestCase):
    def test_long_requires_closed_one_hour_and_four_hour_alignment(self):
        from momentum_compression_service import evaluate_htf_alignment
        calls = []

        def fetch(symbol, interval, limit, **kwargs):
            calls.append((symbol, interval, limit, kwargs))
            return trend_frame("LONG")

        result = evaluate_htf_alignment("KEEPUSDT", "LONG", fetch=fetch)

        self.assertEqual(result["alignment"], "CONFIRMED")
        self.assertEqual([call[1] for call in calls], ["1h", "4h"])
        self.assertTrue(all(call[3]["closed_only"] for call in calls))
        self.assertTrue(all(call[3]["market_type"] == "futures" for call in calls))

    def test_htf_conflict_and_insufficient_data_are_distinct(self):
        from momentum_compression_service import evaluate_htf_alignment

        def conflict_fetch(symbol, interval, limit, **kwargs):
            return trend_frame("LONG" if interval == "1h" else "SHORT")

        self.assertEqual(evaluate_htf_alignment("KEEPUSDT", "LONG", fetch=conflict_fetch)["alignment"], "CONFLICT")
        self.assertEqual(evaluate_htf_alignment("KEEPUSDT", "LONG", fetch=lambda *args, **kwargs: None)["alignment"], "UNKNOWN")


class CompressionScanFailureIsolationTests(unittest.TestCase):
    def test_symbol_data_failure_preserves_existing_pool_and_marks_unavailable(self):
        from momentum_compression_service import scan_compression_market

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / "state.json"
            state = default_state()
            state["pool"]["keep-long"] = pool_item()
            save_compression_state(state_path, state)
            with patch("momentum_compression_service.fetch_compression_universe", return_value=(["KEEPUSDT"], {})), \
                 patch("momentum_compression_service.fetch_klines", return_value=None):
                report = scan_compression_market(state_path, root)
            stored = load_compression_state(state_path)

        self.assertEqual(report["errors"], 1)
        self.assertIn("keep-long", stored["pool"])
        self.assertEqual(stored["pool"]["keep-long"]["data_status"], "unavailable")

    def test_universe_failure_does_not_save_or_change_old_state_bytes(self):
        from momentum_compression_service import scan_compression_market

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / "state.json"
            state = default_state()
            state["pool"]["keep-long"] = pool_item()
            save_compression_state(state_path, state)
            original = state_path.read_bytes()
            with patch("momentum_compression_service.fetch_compression_universe", side_effect=RuntimeError("offline")), \
                 patch("momentum_compression_service.save_compression_state") as save:
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    scan_compression_market(state_path, root)
            self.assertFalse(save.called)
            self.assertEqual(state_path.read_bytes(), original)

    def test_repeated_fixed_scan_keeps_the_same_compression_identity(self):
        from momentum_compression_service import scan_compression_market

        frame = trend_frame("LONG", 220)
        candidate = eligible_evaluation()
        rejected = {**eligible_evaluation(), "side": "SHORT", "compression_id": "keep-short", "state": "REJECTED"}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / "state.json"
            with patch("momentum_compression_service.fetch_compression_universe", return_value=(["KEEPUSDT"], {})), \
                 patch("momentum_compression_service.fetch_klines", return_value=frame), \
                 patch("momentum_compression_service.evaluate_both_sides", return_value=[candidate, rejected]), \
                 patch("momentum_compression_service.evaluate_htf_alignment", return_value={"alignment": "CONFIRMED"}):
                first = scan_compression_market(state_path, root)
                second = scan_compression_market(state_path, root)
            stored = load_compression_state(state_path)

        self.assertEqual([row["compression_id"] for row in first["rows"]], ["keep-long"])
        self.assertEqual([row["compression_id"] for row in second["rows"]], ["keep-long"])
        self.assertEqual(list(stored["pool"]), ["keep-long"])


if __name__ == "__main__":
    unittest.main()
