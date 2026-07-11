import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from backtest.data_store import HistoricalStore
from backtest.bitget_history import BitgetHistorySource
from backtest.execution import SimBroker
from backtest.engine import ReplayEngine
from backtest.experiment import ExperimentSpec
from backtest.cli import build_run_id
from backtest.metrics import summarize_positions
from strategy_core import ExitRules


class ReplayBot:
    def _rj_only_signal_from_df(self, symbol, interval, candles):
        if len(candles) < 60:
            return None
        return {
            "direction": "LONG",
            "price": float(candles["c"].iloc[-1]),
            "rj_only_stop_price": float(candles["c"].iloc[-1]) - 2.0,
            "rj_only_key_time": int(candles["ot"].iloc[-2]),
            "rj_only_confirm_time": int(candles["ot"].iloc[-1]),
            "rj_trigger_source": "j0_recover",
            "signal_key": "RJ|TEST|LONG|ONE",
            "rj_volume_filter_pass": True,
            "rj_sr_near_support": True,
            "rj_sr_bull_div": True,
        }


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"code": "00000", "data": self.data}


class FakeHistorySession:
    def get(self, _url, params, timeout):
        del timeout
        end = int(params["endTime"])
        if end > 200:
            return FakeResponse([["300", "1", "2", "0", "1", "10"], ["200", "1", "2", "0", "1", "10"]])
        if end == 200:
            return FakeResponse([["100", "1", "2", "0", "1", "10"]])
        return FakeResponse([])


class BacktestSystemTest(unittest.TestCase):
    def test_run_id_changes_with_symbol_or_data(self):
        base = build_run_id("manifest", "BTCUSDT", ["a", "b"])

        self.assertNotEqual(base, build_run_id("manifest", "NEARUSDT", ["a", "b"]))
        self.assertNotEqual(base, build_run_id("manifest", "BTCUSDT", ["a", "c"]))

    def test_bitget_pagination_keeps_boundary_candle(self):
        source = BitgetHistorySource(session=FakeHistorySession(), request_pause=0)

        frame = source.fetch_candles("TESTUSDT", "1m", 100, 400)

        self.assertEqual(frame["ot"].tolist(), [100, 200, 300])

    def test_store_round_trip_detects_gaps_and_point_in_time_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp))
            frame = pd.DataFrame({
                "ot": [0, 60_000, 180_000],
                "o": [1, 2, 3], "h": [2, 3, 4], "l": [0, 1, 2],
                "c": [1.5, 2.5, 3.5], "v": [10, 20, 30],
            })
            store.write_candles("bitget", "TESTUSDT", "1m", frame, "fixture-v1")
            store.write_contracts([
                {"symbol": "OLDUSDT", "listed_at": 0, "delisted_at": 120_000},
                {"symbol": "NEWUSDT", "listed_at": 120_000, "delisted_at": None},
            ])

            loaded = store.read_candles("bitget", "TESTUSDT", "1m")

            self.assertEqual(loaded["ot"].tolist(), [0, 60_000, 180_000])
            self.assertEqual(store.find_gaps(loaded, 60_000), [(120_000, 120_000)])
            self.assertEqual(store.universe_at(60_000), ["OLDUSDT"])
            self.assertEqual(store.universe_at(180_000), ["NEWUSDT"])

    def test_sim_broker_reconciles_partial_fills_fees_and_equity(self):
        broker = SimBroker(initial_equity=1000.0, fee_rate=0.001, slippage_bps=10)

        entry = broker.open_market("TESTUSDT", "LONG", 100.0, 2.0, 1)
        partial = broker.close_market("TESTUSDT", 1.0, 110.0, 2, "tier2")
        final = broker.close_market("TESTUSDT", 1.0, 105.0, 3, "trail")

        self.assertAlmostEqual(entry.price, 100.1)
        self.assertAlmostEqual(partial.price, 109.89)
        self.assertAlmostEqual(final.price, 104.895)
        expected = 1000.0 + (109.89 - 100.1) + (104.895 - 100.1)
        expected -= entry.fee + partial.fee + final.fee
        self.assertAlmostEqual(broker.equity, expected)
        self.assertAlmostEqual(broker.position_quantity("TESTUSDT"), 0.0)

    def test_metrics_count_partial_fills_as_one_position(self):
        events = [
            {"type": "entry_fill", "position_id": "p1", "risk_usdt": 10.0, "fee": 1.0},
            {"type": "exit_fill", "position_id": "p1", "net_pnl": 12.0, "fee": 0.5, "mfe_r": 2.0, "mae_r": 0.4},
            {"type": "exit_fill", "position_id": "p1", "net_pnl": 8.0, "fee": 0.5, "mfe_r": 3.0, "mae_r": 0.4},
            {"type": "entry_fill", "position_id": "p2", "risk_usdt": 10.0, "fee": 1.0},
            {"type": "exit_fill", "position_id": "p2", "net_pnl": -10.0, "fee": 0.5, "mfe_r": 0.2, "mae_r": 1.0},
        ]

        metrics = summarize_positions(events)

        self.assertEqual(metrics["trades"], 2)
        self.assertEqual(metrics["wins"], 1)
        self.assertAlmostEqual(metrics["sum_r"], 0.8)
        self.assertAlmostEqual(metrics["mean_r"], 0.4)
        self.assertEqual(metrics["reach_2r"], 1)

    def test_experiment_manifest_rejects_overlap_and_mutation(self):
        with self.assertRaisesRegex(ValueError, "period_overlap"):
            ExperimentSpec(
                name="bad", rules={"rj": "v1"}, data_version="d1",
                train=(0, 100), validation=(90, 200), test=(200, 300),
            ).validate()

        spec = ExperimentSpec(
            name="A", rules={"rj": "v1", "timeframe": "30m"}, data_version="d1",
            train=(0, 100), validation=(100, 200), test=(200, 300),
        )
        frozen = spec.freeze()
        payload = json.loads(frozen)
        self.assertEqual(payload["rules"]["timeframe"], "30m")
        self.assertEqual(payload["manifest_hash"], spec.manifest_hash())

    def test_replay_uses_next_one_minute_open_and_dedupes_signal_key(self):
        opens = [index * 1_800_000 for index in range(62)]
        frame_30m = pd.DataFrame({
            "ot": opens,
            "o": [100.0] * 62, "h": [101.0] * 62, "l": [99.0] * 62,
            "c": [100.0] * 62, "v": [1000.0] * 62,
        })
        first_decision = opens[59] + 1_800_000
        minute_opens = [first_decision + index * 60_000 for index in range(65)]
        frame_1m = pd.DataFrame({
            "ot": minute_opens,
            "o": [101.0] + [102.0] * 64,
            "h": [101.5] + [104.0] * 64,
            "l": [100.5] + [101.0] * 64,
            "c": [101.2] + [103.5] * 64,
            "v": [100.0] * 65,
        })
        engine = ReplayEngine(
            bot=ReplayBot(), initial_equity=1000.0, risk_usdt=10.0,
            fee_rate=0.0, slippage_bps=0.0, exit_rules=ExitRules(), warmup_bars=60,
        )

        result = engine.run_symbol(
            "TESTUSDT", frame_30m, frame_1m,
            btc_stage_provider=lambda _: {"stage": "range", "direction": "range", "extreme_veto": False},
        )

        entries = [event for event in result.events if event["type"] == "entry_fill"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["time"], first_decision)
        self.assertEqual(entries[0]["price"], 101.0)
        self.assertEqual(result.used_signal_keys, {"RJ|TEST|LONG|ONE"})


if __name__ == "__main__":
    unittest.main()
