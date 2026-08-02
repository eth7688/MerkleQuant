import json
import inspect
import os
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import strategy_filters
from strategy_core import StrategySnapshot, evaluate_rj_entry

os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
from trader import Position, SqueezeBreakoutBot, TradeConfig
from backtest.bitget_history import BitgetHistorySource
import backtest.cli as backtest_cli
from backtest.data_store import HistoricalStore
from backtest.engine import PortfolioReplayEngine, ReplayEngine
from backtest.metrics import summarize_positions
from strategy_core import ExitRules


DAY_MS = 86_400_000


def daily_frame(last_rows):
    frame = pd.DataFrame({
        "ot": [index * DAY_MS for index in range(40)],
        "o": [100.0] * 40,
        "h": [101.0] * 40,
        "l": [99.0] * 40,
        "c": [100.0] * 40,
        "v": [100.0] * 40,
    })
    start = len(frame) - len(last_rows)
    for index, values in enumerate(last_rows, start=start):
        frame.loc[index, ["o", "h", "l", "c", "v"]] = values
    return frame


class DailyPatternStateTest(unittest.TestCase):
    def test_uses_only_daily_candles_closed_by_the_decision_time(self):
        evaluate = getattr(strategy_filters, "evaluate_daily_pattern_state", None)
        self.assertTrue(callable(evaluate), "evaluate_daily_pattern_state is required")
        frame = daily_frame([
            (101.0, 102.0, 98.0, 99.0, 100.0),
            (98.5, 102.0, 98.0, 101.5, 100.0),
            (102.0, 103.0, 97.0, 98.0, 100.0),
        ])
        decision_time = int(frame["ot"].iloc[-1]) + DAY_MS // 2

        state = evaluate(frame, "LONG", decision_time=decision_time)

        self.assertTrue(state["recorded"])
        self.assertEqual(state["kind"], "bullish_engulfing")
        self.assertEqual(state["pattern_direction"], "LONG")
        self.assertEqual(state["alignment"], "aligned")
        self.assertFalse(state["would_block"])
        self.assertEqual(state["candle_open_time"], int(frame["ot"].iloc[-2]))
        self.assertEqual(state["candle_close_time"], int(frame["ot"].iloc[-2]) + DAY_MS)

    def test_soft_filter_marks_only_explicit_opposite_rank_two_pattern(self):
        evaluate = getattr(strategy_filters, "evaluate_daily_pattern_state", None)
        self.assertTrue(callable(evaluate), "evaluate_daily_pattern_state is required")
        frame = daily_frame([
            (99.0, 102.0, 98.0, 101.0, 100.0),
            (101.5, 102.0, 98.0, 98.5, 100.0),
        ])

        state = evaluate(frame, "LONG", decision_time=40 * DAY_MS)

        self.assertEqual(state["kind"], "bearish_engulfing")
        self.assertEqual(state["pattern_direction"], "SHORT")
        self.assertEqual(state["alignment"], "opposed")
        self.assertEqual(state["rank"], 2)
        self.assertTrue(state["would_block"])

    def test_missing_daily_history_fails_open_but_remains_auditable(self):
        evaluate = getattr(strategy_filters, "evaluate_daily_pattern_state", None)
        self.assertTrue(callable(evaluate), "evaluate_daily_pattern_state is required")

        state = evaluate(pd.DataFrame(), "SHORT", decision_time=40 * DAY_MS)

        self.assertFalse(state["recorded"])
        self.assertFalse(state["would_block"])
        self.assertEqual(state["reason"], "daily_history_unavailable")
        self.assertEqual(state["alignment"], "unavailable")


class DailyPatternStrategyGateTest(unittest.TestCase):
    def _bot(self, mode):
        class Bot:
            cfg = SimpleNamespace(
                rj_only_stats_enabled=False,
                min_score=0.0,
                rj_daily_pattern_filter_mode=mode,
            )

            @staticmethod
            def _rj_only_signal_from_df(symbol, interval, candles):
                return {
                    "symbol": symbol,
                    "direction": "LONG",
                    "price": float(candles["c"].iloc[-1]),
                    "rj_only_stop_price": float(candles["c"].iloc[-1]) - 2.0,
                    "signal_key": "RJ|TESTUSDT|LONG|ONE",
                    "score": 80.0,
                    "rj_volume_filter_pass": True,
                    "rj_sr_near_support": True,
                    "rj_sr_bull_div": True,
                }

        return Bot()

    def _snapshot(self):
        self.assertIn("candles_1d", {item.name for item in fields(StrategySnapshot)})
        candles_30m = pd.DataFrame({
            "ot": [0, 1_800_000, 3_600_000],
            "o": [100.0, 100.0, 100.0],
            "h": [101.0, 101.0, 101.0],
            "l": [99.0, 99.0, 99.0],
            "c": [100.0, 100.0, 100.0],
            "v": [100.0, 100.0, 100.0],
        })
        bearish = daily_frame([
            (99.0, 102.0, 98.0, 101.0, 100.0),
            (101.5, 102.0, 98.0, 98.5, 100.0),
        ])
        return StrategySnapshot(
            "TESTUSDT", "30m", 40 * DAY_MS, candles_30m,
            {"stage": "range", "direction": "range", "extreme_veto": False},
            candles_1d=bearish,
        )

    def test_log_only_records_opposite_pattern_without_blocking(self):
        decision = evaluate_rj_entry(self._bot("log_only"), self._snapshot())

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.evidence["daily_pattern"]["kind"], "bearish_engulfing")
        self.assertTrue(decision.evidence["daily_pattern"]["would_block"])

    def test_soft_mode_blocks_only_opposite_pattern(self):
        decision = evaluate_rj_entry(self._bot("soft"), self._snapshot())

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily_pattern_opposed")
        self.assertEqual(decision.evidence["daily_pattern"]["alignment"], "opposed")


class DailyPatternLiveAuditTest(unittest.TestCase):
    def _position(self):
        self.assertIn("daily_pattern", {item.name for item in fields(Position)})
        return Position(
            symbol="TESTUSDT",
            direction="LONG",
            entry_price=100.0,
            entry_time=datetime(2026, 8, 1, 12, 0),
            quantity=1.0,
            sl_price=98.0,
            current_sl=98.0,
            risk_usdt=2.0,
            signal_score=80.0,
            initial_band_hi=100.0,
            initial_band_lo=100.0,
            daily_pattern={
                "recorded": True,
                "kind": "bullish_engulfing",
                "pattern_direction": "LONG",
                "alignment": "aligned",
                "rank": 2,
                "would_block": False,
                "candle_open_time": 123,
                "candle_close_time": 456,
                "reason": "aligned",
                "mode": "log_only",
            },
        )

    def test_only_demo_normalization_enables_log_only_observation(self):
        cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        from web_ui import UserBotManager

        self.assertEqual(cfg.rj_daily_pattern_filter_mode, "off")
        normalized = UserBotManager.__new__(UserBotManager)._normalize_demo_config(cfg)
        self.assertIs(normalized, cfg)
        self.assertEqual(normalized.rj_daily_pattern_filter_mode, "log_only")

    def test_live_fetch_uses_closed_bitget_utc_daily_candles(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="binance")
        bot.cfg.rj_daily_pattern_filter_mode = "log_only"
        fetch_state = getattr(bot, "_daily_pattern_state_for_entry", None)
        self.assertTrue(callable(fetch_state), "live daily pattern fetch is required")
        frame = daily_frame([
            (101.0, 102.0, 98.0, 99.0, 100.0),
            (98.5, 102.0, 98.0, 101.5, 100.0),
        ])

        with patch("trader.fetch_klines", return_value=frame) as fetch:
            state = fetch_state("TESTUSDT", "LONG")

        self.assertEqual(state["kind"], "bullish_engulfing")
        self.assertEqual(state["mode"], "log_only")
        fetch.assert_called_once_with(
            "TESTUSDT", "1d", 50,
            exchange="bitget", closed_only=True, bitget_granularity="1Dutc",
        )

    def test_live_daily_fetch_failure_is_recorded_and_fails_open(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "log_only"

        with patch("trader.fetch_klines", side_effect=RuntimeError("network")):
            state = bot._daily_pattern_state_for_entry("TESTUSDT", "LONG")

        self.assertFalse(state["recorded"])
        self.assertFalse(state["would_block"])
        self.assertEqual(state["reason"], "daily_fetch_failed")
        self.assertEqual(state["mode"], "log_only")

    def test_predicta_entry_observes_daily_pattern_when_log_only(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "log_only"
        snapshot = {"recorded": True, "kind": "hammer", "mode": "log_only",
                    "would_block": False}
        with patch.object(bot, "_daily_pattern_state_for_entry", return_value=snapshot) as observe:
            state, blocked = bot._daily_pattern_entry_decision(
                "TESTUSDT", "LONG", "predicta_ewo", decision_time=123,
            )
        observe.assert_called_once_with("TESTUSDT", "LONG", decision_time=123)
        self.assertIs(state, snapshot)
        self.assertFalse(blocked)

    def test_off_mode_skips_daily_fetch_for_every_source(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "off"
        with patch.object(bot, "_daily_pattern_state_for_entry") as observe:
            state, blocked = bot._daily_pattern_entry_decision(
                "TESTUSDT", "SHORT", "predicta_ewo", decision_time=123,
            )
        observe.assert_not_called()
        self.assertIsNone(state)
        self.assertFalse(blocked)

    def test_soft_block_remains_rj_only(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)
        bot.cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget")
        bot.cfg.rj_daily_pattern_filter_mode = "soft"
        opposed = {"recorded": True, "kind": "bearish_engulfing", "mode": "soft",
                   "would_block": True}
        with patch.object(bot, "_daily_pattern_state_for_entry", return_value=opposed):
            _, predicta_block = bot._daily_pattern_entry_decision(
                "TESTUSDT", "LONG", "predicta_ewo", decision_time=123,
            )
            _, rj_block = bot._daily_pattern_entry_decision(
                "TESTUSDT", "LONG", "rj_only", decision_time=123,
            )
        self.assertFalse(predicta_block)
        self.assertTrue(rj_block)

    def test_position_daily_pattern_is_saved_and_restored(self):
        bot = SqueezeBreakoutBot(TradeConfig(mode="paper", enabled=False, exchange="bitget"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"
            bot._positions_path = str(path)
            bot.positions = [self._position()]

            self.assertTrue(bot._save_positions())
            payload = json.loads(path.read_text(encoding="utf-8"))
            restored = bot._load_positions()

        self.assertEqual(payload[0]["daily_pattern"]["kind"], "bullish_engulfing")
        self.assertEqual(restored[0].daily_pattern["alignment"], "aligned")

    def test_malformed_legacy_daily_pattern_rank_does_not_break_restore(self):
        bot = SqueezeBreakoutBot.__new__(SqueezeBreakoutBot)

        state = bot._position_daily_pattern({"recorded": True, "rank": "legacy-invalid"})

        self.assertEqual(state["rank"], 0)

    def test_cards_and_trade_lists_render_the_persisted_daily_pattern(self):
        source = (Path(__file__).resolve().parents[1] / "web_ui.py").read_text(encoding="utf-8")

        self.assertIn("昨日收盘", source)
        self.assertIn("UTC收盘", source)
        self.assertGreaterEqual(source.count("dailyPatternLine(p.daily_pattern)"), 2)
        self.assertGreaterEqual(source.count("dailyPatternTradeTag(t.daily_pattern)"), 2)

    def test_exit_trade_record_copies_entry_daily_pattern_snapshot(self):
        source = (Path(__file__).resolve().parents[1] / "trader.py").read_text(encoding="utf-8")

        self.assertIn("'daily_pattern': self._position_daily_pattern(getattr(pos, 'daily_pattern', {}))", source)


class DailyPatternBacktestTest(unittest.TestCase):
    def test_existing_backtests_do_not_require_daily_history_when_filter_is_off(self):
        enabled = getattr(backtest_cli, "_daily_pattern_enabled", None)
        self.assertTrue(callable(enabled), "backtest daily-mode gate is required")
        self.assertFalse(enabled({}))
        self.assertFalse(enabled({"rj_daily_pattern_filter_mode": "off"}))
        self.assertTrue(enabled({"rj_daily_pattern_filter_mode": "log_only"}))
        self.assertTrue(enabled({"rj_daily_pattern_filter_mode": "soft"}))

    def test_bitget_history_requests_utc_daily_granularity(self):
        calls = []

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"code": "00000", "data": []}

        class Session:
            @staticmethod
            def get(url, params, timeout):
                calls.append((url, params, timeout))
                return Response()

        source = BitgetHistorySource(session=Session(), request_pause=0)
        source.fetch_candles("TESTUSDT", "1d", 0, DAY_MS)

        self.assertEqual(calls[0][1]["granularity"], "1Dutc")

    def test_download_parser_accepts_daily_interval(self):
        args = backtest_cli.build_parser().parse_args([
            "download", "--symbol", "TRXUSDT", "--interval", "1d",
            "--start", "0", "--end", str(DAY_MS), "--data-version", "test",
        ])

        self.assertEqual(args.interval, "1d")

    def test_missing_optional_daily_history_returns_auditable_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(tmp)

            frame, fingerprint = backtest_cli._load_optional_daily_history(store, "TRXUSDT")

        self.assertTrue(frame.empty)
        self.assertEqual(fingerprint, "unavailable:bitget:TRXUSDT:1d")

    def test_present_daily_history_with_missing_manifest_is_a_hard_error(self):
        frame = daily_frame([(99.0, 102.0, 98.0, 101.0, 100.0)])
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(tmp)
            path = store.write_candles("bitget", "TRXUSDT", "1d", frame, "test")
            path.with_suffix(".manifest.json").unlink()

            with self.assertRaises(FileNotFoundError):
                backtest_cli._load_optional_daily_history(store, "TRXUSDT")

    def test_replay_entry_event_contains_point_in_time_daily_snapshot(self):
        class Bot:
            cfg = SimpleNamespace(
                rj_only_stats_enabled=False,
                min_score=0.0,
                rj_daily_pattern_filter_mode="log_only",
            )

            @staticmethod
            def _entry_signal_source():
                return "rj_only"

            @staticmethod
            def _rj_only_signal_from_df(symbol, interval, candles):
                return {
                    "symbol": symbol,
                    "direction": "LONG",
                    "price": float(candles["c"].iloc[-1]),
                    "rj_only_stop_price": float(candles["c"].iloc[-1]) - 2.0,
                    "signal_key": "RJ|TESTUSDT|LONG|ONE",
                    "score": 80.0,
                    "rj_volume_filter_pass": True,
                    "rj_sr_near_support": True,
                    "rj_sr_bull_div": True,
                }

        start = 40 * DAY_MS
        opens = [start + index * 1_800_000 for index in range(61)]
        frame30 = pd.DataFrame({
            "ot": opens,
            "o": [100.0] * 61,
            "h": [101.0] * 61,
            "l": [99.0] * 61,
            "c": [100.0] * 61,
            "v": [1000.0] * 61,
        })
        first_decision = opens[59] + 1_800_000
        frame1 = pd.DataFrame({
            "ot": [first_decision, first_decision + 60_000],
            "o": [100.0, 100.0],
            "h": [100.5, 100.5],
            "l": [99.5, 99.5],
            "c": [100.0, 100.0],
            "v": [10.0, 10.0],
        })
        bearish = daily_frame([
            (99.0, 102.0, 98.0, 101.0, 100.0),
            (101.5, 102.0, 98.0, 98.5, 100.0),
        ])
        engine = ReplayEngine(
            Bot(), initial_equity=1000.0, risk_usdt=10.0,
            fee_rate=0.0, slippage_bps=0.0,
            exit_rules=ExitRules(), warmup_bars=60,
        )
        run_symbol = getattr(engine, "run_symbol")

        result = run_symbol(
            "TESTUSDT", frame30, frame1,
            lambda _: {"stage": "range", "direction": "range", "extreme_veto": False},
            entry_start_ms=first_decision,
            candles_1d=bearish,
        )

        entry = next(event for event in result.events if event["type"] == "entry_fill")
        self.assertEqual(entry["daily_pattern"]["kind"], "bearish_engulfing")
        self.assertTrue(entry["daily_pattern"]["would_block"])

    def test_portfolio_replay_accepts_daily_frames(self):
        parameters = inspect.signature(PortfolioReplayEngine.run).parameters

        self.assertIn("candles_1d", parameters)

    def test_cli_optional_daily_loader_reads_present_valid_history(self):
        frame = daily_frame([(99.0, 102.0, 98.0, 101.0, 100.0)])
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(tmp)
            store.write_candles("bitget", "TRXUSDT", "1d", frame, "test")

            loaded, fingerprint = backtest_cli._load_optional_daily_history(store, "TRXUSDT")

        self.assertEqual(len(loaded), len(frame))
        self.assertEqual(loaded["ot"].tolist(), frame["ot"].tolist())
        self.assertEqual(len(fingerprint), 64)

    def test_metrics_break_down_results_by_daily_pattern_alignment(self):
        events = [
            {"type": "entry_fill", "position_id": "a", "risk_usdt": 10.0, "fee": 0.0,
             "daily_pattern": {"recorded": True, "alignment": "aligned", "kind": "bullish_engulfing"}},
            {"type": "exit_fill", "position_id": "a", "net_pnl": 10.0, "fee": 0.0},
            {"type": "entry_fill", "position_id": "b", "risk_usdt": 10.0, "fee": 0.0,
             "daily_pattern": {"recorded": True, "alignment": "opposed", "kind": "bearish_engulfing"}},
            {"type": "exit_fill", "position_id": "b", "net_pnl": -5.0, "fee": 0.0},
            {"type": "entry_fill", "position_id": "c", "risk_usdt": 10.0, "fee": 0.0,
             "daily_pattern": {"recorded": True, "alignment": "none", "kind": "none"}},
            {"type": "exit_fill", "position_id": "c", "net_pnl": 1.0, "fee": 0.0},
            {"type": "entry_fill", "position_id": "d", "risk_usdt": 10.0, "fee": 0.0,
             "daily_pattern": {"recorded": False, "alignment": "none", "kind": "none"}},
            {"type": "exit_fill", "position_id": "d", "net_pnl": -1.0, "fee": 0.0},
        ]

        summary = summarize_positions(events)

        groups = summary["daily_pattern_breakdown"]
        self.assertEqual(groups["aligned"]["trades"], 1)
        self.assertEqual(groups["aligned"]["win_rate"], 100.0)
        self.assertEqual(groups["aligned"]["mean_r"], 1.0)
        self.assertEqual(groups["opposed"]["trades"], 1)
        self.assertEqual(groups["opposed"]["mean_r"], -0.5)
        self.assertEqual(groups["none"]["trades"], 1)
        self.assertEqual(groups["unavailable"]["trades"], 1)


if __name__ == "__main__":
    unittest.main()
