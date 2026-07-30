import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import momentum_reflow

from momentum_reflow import (
    add_hourly_indicators,
    advance_symbol,
    daily_confirmation,
    fetch_futures_universe,
    load_ledger,
    scan_momentum_reflow,
    save_ledger,
)


def candle_frame(count=80, start=100.0):
    close = np.linspace(start, start + count - 1, count)
    return pd.DataFrame({
        "ot": np.arange(count, dtype=np.int64) * 3_600_000,
        "o": close - 0.4,
        "h": close + 1.0,
        "l": close - 1.0,
        "c": close,
        "v": np.full(count, 100.0),
    })


class HourlyIndicatorTests(unittest.TestCase):
    def test_adds_ema50_atr14_and_prior_volume_average(self):
        out = add_hourly_indicators(candle_frame())
        self.assertEqual(
            {"ema50", "atr14", "vol_ma20_prev"}.issubset(out.columns),
            True,
        )
        self.assertTrue(np.isfinite(out.iloc[-1]["ema50"]))
        self.assertTrue(np.isfinite(out.iloc[-1]["atr14"]))
        self.assertEqual(out.iloc[-1]["vol_ma20_prev"], 100.0)

    def test_volume_average_excludes_current_breakout_candle(self):
        frame = candle_frame()
        frame.loc[frame.index[-1], "v"] = 1_000.0
        out = add_hourly_indicators(frame)
        self.assertEqual(out.iloc[-1]["vol_ma20_prev"], 100.0)


PATTERN_ROWS = {
    "bullish_engulfing": [
        (101.0, 102.0, 98.0, 99.0),
        (98.5, 102.0, 98.0, 101.5),
    ],
    "bearish_engulfing": [
        (99.0, 102.0, 98.0, 101.0),
        (101.5, 102.0, 98.0, 98.5),
    ],
    "hammer": [
        (100.5, 101.2, 98.5, 101.0),
    ],
    "shooting_star": [
        (100.5, 102.5, 99.8, 100.0),
    ],
    "morning_star": [
        (102.0, 102.5, 97.5, 98.0),
        (99.2, 100.0, 98.8, 99.6),
        (99.5, 101.0, 99.0, 100.5),
    ],
    "evening_star": [
        (98.0, 102.5, 97.5, 102.0),
        (100.4, 101.2, 100.0, 100.8),
        (100.5, 101.0, 99.0, 99.5),
    ],
}


def make_daily_pattern(kind):
    frame = pd.DataFrame({
        "ot": np.arange(40, dtype=np.int64) * 86_400_000,
        "o": np.full(40, 100.0),
        "h": np.full(40, 101.0),
        "l": np.full(40, 99.0),
        "c": np.full(40, 100.0),
        "v": np.full(40, 100.0),
    })
    values = PATTERN_ROWS[kind]
    start = len(frame) - len(values)
    for index, (open_, high, low, close) in enumerate(values, start=start):
        frame.loc[index, ["o", "h", "l", "c"]] = [open_, high, low, close]
    return frame


class DailyConfirmationTests(unittest.TestCase):
    def test_directional_strong_daily_candle(self):
        frame = candle_frame(40, start=50.0)
        frame.loc[frame.index[-1], ["o", "h", "l", "c", "v"]] = [
            100.0, 112.0, 99.0, 111.0, 300.0
        ]
        result = daily_confirmation(frame, "LONG")
        self.assertEqual(result, {"passed": True, "kind": "strong_momentum", "rank": 3})
        self.assertFalse(daily_confirmation(frame, "SHORT")["passed"])

    def test_large_wick_candle_does_not_pass_strong_momentum(self):
        frame = make_daily_pattern("hammer")
        frame.loc[frame.index[-1], ["o", "h", "l", "c", "v"]] = [
            100.0, 120.0, 99.0, 103.0, 300.0
        ]
        self.assertEqual(
            daily_confirmation(frame, "LONG"),
            {"passed": False, "kind": "none", "rank": 0},
        )

    def test_opposite_strong_candle_still_allows_bottom_fractal(self):
        frame = candle_frame(40)
        frame.loc[37, "l"], frame.loc[38, "l"] = 90.0, 80.0
        frame.loc[39, ["o", "h", "l", "c", "v"]] = [
            111.0, 112.0, 96.0, 100.0, 300.0
        ]
        self.assertEqual(
            daily_confirmation(frame, "LONG"),
            {"passed": True, "kind": "bottom_fractal", "rank": 1},
        )

    def test_confirmed_bottom_and_top_fractals(self):
        bottom = candle_frame(40)
        bottom.loc[37, "l"], bottom.loc[38, "l"], bottom.loc[39, "l"] = 90.0, 80.0, 91.0
        self.assertEqual(
            daily_confirmation(bottom, "LONG")["kind"],
            "bottom_fractal",
        )
        top = candle_frame(40)
        top.loc[37, "h"], top.loc[38, "h"], top.loc[39, "h"] = 110.0, 120.0, 109.0
        self.assertEqual(
            daily_confirmation(top, "SHORT")["kind"],
            "top_fractal",
        )

    def test_reversal_pattern_matrix(self):
        expected = {
            "LONG": {"bullish_engulfing", "hammer", "morning_star"},
            "SHORT": {"bearish_engulfing", "shooting_star", "evening_star"},
        }
        for direction, kinds in expected.items():
            observed = {
                daily_confirmation(make_daily_pattern(kind), direction)["kind"]
                for kind in kinds
            }
            self.assertEqual(observed, kinds)

    def test_rejects_insufficient_history(self):
        result = daily_confirmation(candle_frame(2), "LONG")
        self.assertEqual(result, {"passed": False, "kind": "none", "rank": 0})


HOUR_MS = 3_600_000
BASE_OT = 100 * HOUR_MS


def make_waiting_state(direction):
    return {
        "last_processed_open_time": BASE_OT,
        "event": {
            "direction": direction,
            "state": "WAIT_FIRST_RETURN",
            "breakout_open_time": BASE_OT - 4 * HOUR_MS,
            "breakout_volume_ratio": 2.0,
            "expansion_time": BASE_OT - 3 * HOUR_MS,
            "max_expansion_atr": 2.0,
            "first_touch_time": 0,
            "return_window_index": 0,
            "audit_reason": "expansion_confirmed",
        },
    }


def make_touch_frame(close=100.1, ema50=100.0, atr14=1.0, offset=1, direction="LONG"):
    target = BASE_OT + offset * HOUR_MS
    ema_values = (
        [ema50 - 0.3, ema50 - 0.2, ema50 - 0.1, ema50]
        if direction == "LONG"
        else [ema50 + 0.3, ema50 + 0.2, ema50 + 0.1, ema50]
    )
    rows = []
    for index, ema_value in enumerate(ema_values):
        row_close = close if index == 3 else ema_values[index]
        rows.append(
            {
                "ot": target - (3 - index) * HOUR_MS,
                "o": row_close,
                "h": max(row_close, ema_value + 0.1),
                "l": min(row_close, ema_value - 0.1),
                "c": row_close,
                "v": 100.0,
                "ema50": ema_value,
                "atr14": atr14,
                "vol_ma20_prev": 100.0,
            }
        )
    rows[-1]["h"] = max(close, ema50 + 0.2 * atr14)
    rows[-1]["l"] = min(close, ema50 - 0.2 * atr14)
    return pd.DataFrame(rows)


def make_far_frame(offset=2):
    frame = make_touch_frame(close=102.0, offset=offset)
    frame.loc[frame.index[-1], ["h", "l"]] = [102.2, 101.8]
    return frame


def make_state_machine_frame(direction, expansion_offset=0, start_offset=-5):
    sign = 1.0 if direction == "LONG" else -1.0
    ema_values = [99.7, 99.8, 99.9, 100.0, 100.1, 100.2, 100.3, 100.4]
    if direction == "SHORT":
        ema_values = list(reversed(ema_values))
    closes = [value - 0.1 * sign for value in ema_values]
    breakout_index = 4
    closes[breakout_index] = ema_values[breakout_index] + (1.6 if expansion_offset == 0 else 0.1) * sign
    for index in range(1, 4):
        follow_index = breakout_index + index
        if follow_index >= len(closes):
            break
        closes[follow_index] = ema_values[follow_index] + (1.6 if expansion_offset == index else 0.1) * sign
    rows = []
    for index, (ema_value, close_value) in enumerate(zip(ema_values, closes)):
        open_value = close_value
        volume = 100.0
        if index == breakout_index:
            open_value = ema_value - 1.0 * sign
            volume = 200.0
        high = max(open_value, close_value) + 0.1
        low = min(open_value, close_value) - 0.1
        if index == len(ema_values) - 1:
            high = max(high, ema_value + 0.2)
            low = min(low, ema_value - 0.2)
        rows.append(
            {
                "ot": (BASE_OT + start_offset * HOUR_MS) + index * HOUR_MS,
                "o": open_value,
                "h": high,
                "l": low,
                "c": close_value,
                "v": volume,
                "ema50": ema_value,
                "atr14": 1.0,
                "vol_ma20_prev": 100.0,
            }
        )
    return pd.DataFrame(rows)


class ReflowStateMachineTests(unittest.TestCase):
    def test_event_persists_breakout_close_time(self):
        state, _ = advance_symbol("TESTUSDT", {}, make_state_machine_frame("LONG").iloc[:5])

        self.assertEqual(
            state["event"]["breakout_close_time"],
            state["event"]["breakout_open_time"] + HOUR_MS,
        )

    def test_long_and_short_breakout_expand_then_open_first_return_window(self):
        for direction in ("LONG", "SHORT"):
            frame = make_state_machine_frame(direction).iloc[:6]
            state, candidate = advance_symbol("TESTUSDT", {}, frame)
            self.assertEqual(state["event"]["state"], "RETURN_WINDOW")
            self.assertEqual(state["event"]["return_window_index"], 1)
            self.assertEqual(candidate["direction"], direction)
            self.assertEqual(candidate["window_index"], 1)

    def test_expansion_can_confirm_on_breakout_or_each_follow_up_candle(self):
        for expansion_offset in range(4):
            state, candidate = advance_symbol(
                "TESTUSDT", {}, make_state_machine_frame("LONG", expansion_offset).iloc[: 5 + expansion_offset]
            )
            self.assertEqual(state["event"]["state"], "WAIT_FIRST_RETURN")
            self.assertEqual(state["event"]["expansion_time"], BASE_OT + (expansion_offset - 1) * HOUR_MS)
            self.assertIsNone(candidate)

    def test_third_follow_up_without_expansion_invalidates_event(self):
        state, candidate = advance_symbol("TESTUSDT", {}, make_state_machine_frame("LONG", 99).iloc[:8])
        self.assertEqual(state["event"]["state"], "INVALIDATED")
        self.assertEqual(state["event"]["audit_reason"], "expansion_timeout")
        self.assertIsNone(candidate)

    def test_wait_first_return_has_no_elapsed_time_expiry(self):
        frame = make_far_frame(offset=100)
        frame.loc[frame.index[:3], "ot"] = [BASE_OT - 3 * HOUR_MS, BASE_OT - 2 * HOUR_MS, BASE_OT - HOUR_MS]
        state, candidate = advance_symbol(
            "TESTUSDT", make_waiting_state("LONG"), frame
        )
        self.assertEqual(state["event"]["state"], "WAIT_FIRST_RETURN")
        self.assertIsNone(candidate)

    def test_slope_reversal_invalidates_before_first_touch(self):
        frame = make_far_frame()
        frame.loc[frame.index[:3], "ot"] = [BASE_OT - 3 * HOUR_MS, BASE_OT - 2 * HOUR_MS, BASE_OT - HOUR_MS]
        frame.loc[:, "ema50"] = [100.3, 100.2, 100.1, 100.0]
        state, candidate = advance_symbol("TESTUSDT", make_waiting_state("LONG"), frame)
        self.assertEqual(state["event"]["state"], "INVALIDATED")
        self.assertEqual(state["event"]["audit_reason"], "ema_slope_reversal")
        self.assertIsNone(candidate)

    def test_close_may_finish_on_either_side_of_ema(self):
        for close in (99.70, 100.30):
            state, candidate = advance_symbol(
                "TESTUSDT", make_waiting_state("LONG"), make_touch_frame(close=close)
            )
            self.assertIsNotNone(candidate)
            self.assertEqual(state["event"]["state"], "RETURN_WINDOW")

    def test_exact_close_distance_boundary_remains_eligible(self):
        state, candidate = advance_symbol(
            "TESTUSDT", make_waiting_state("LONG"), make_touch_frame(close=100.35)
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(state["event"]["state"], "RETURN_WINDOW")

    def test_wait_first_return_keeps_later_maximum_expansion(self):
        frame = make_state_machine_frame("LONG").iloc[:7].copy()
        expansion_row = frame.index[5]
        frame.loc[expansion_row, ["o", "h", "l", "c"]] = [102.3, 102.4, 102.2, 102.3]
        return_row = frame.index[6]
        ema50 = frame.loc[return_row, "ema50"]
        frame.loc[return_row, ["o", "h", "l", "c"]] = [ema50, ema50 + 0.1, ema50 - 0.1, ema50]

        state, candidate = advance_symbol("TESTUSDT", {}, frame)

        self.assertIsNotNone(candidate)
        self.assertEqual(state["event"]["state"], "RETURN_WINDOW")
        self.assertGreaterEqual(state["event"]["max_expansion_atr"], 2.0)

    def test_first_bad_touch_consumes_event(self):
        state, candidate = advance_symbol(
            "TESTUSDT", make_waiting_state("LONG"), make_touch_frame(close=100.36)
        )
        self.assertIsNone(candidate)
        self.assertEqual(state["event"]["state"], "CONSUMED")
        self.assertEqual(state["event"]["audit_reason"], "first_touch_close_too_far")

    def test_return_window_is_consecutive_and_capped_at_five(self):
        state = make_waiting_state("SHORT")
        for expected_index in range(1, 6):
            state, candidate = advance_symbol(
                "TESTUSDT",
                state,
                make_touch_frame(close=99.9, offset=expected_index, direction="SHORT"),
            )
            self.assertEqual(candidate["window_index"], expected_index)
        self.assertEqual(state["event"]["state"], "CONSUMED")

    def test_leaving_zone_consumes_and_never_reopens_same_event(self):
        state, _ = advance_symbol(
            "TESTUSDT", make_waiting_state("LONG"), make_touch_frame(close=100.1)
        )
        state, candidate = advance_symbol("TESTUSDT", state, make_far_frame())
        self.assertIsNone(candidate)
        self.assertEqual(state["event"]["state"], "CONSUMED")
        state, candidate = advance_symbol("TESTUSDT", state, make_touch_frame(close=100.1, offset=3))
        self.assertIsNone(candidate)

    def test_repeated_scan_returns_active_candidate_without_incrementing_twice(self):
        frame = make_touch_frame(close=100.1)
        state, first = advance_symbol("TESTUSDT", make_waiting_state("LONG"), frame)
        repeated_state, repeated = advance_symbol("TESTUSDT", state, frame)
        self.assertEqual(repeated_state, state)
        self.assertEqual(repeated, first)

    def test_later_strong_breakout_replaces_each_terminal_event(self):
        for terminal_state in ("CONSUMED", "INVALIDATED"):
            terminal = make_waiting_state("LONG")
            terminal["event"]["state"] = terminal_state
            frame = make_state_machine_frame("SHORT", 0, start_offset=1).iloc[:5]
            state, candidate = advance_symbol("TESTUSDT", terminal, frame)
            self.assertEqual(state["event"]["direction"], "SHORT")
            self.assertEqual(state["event"]["state"], "WAIT_FIRST_RETURN")
            self.assertIsNone(candidate)

    def test_non_finite_indicator_row_does_not_advance_cursor(self):
        frame = make_touch_frame()
        frame.loc[frame.index[-1], "atr14"] = float("nan")
        state, candidate = advance_symbol("TESTUSDT", make_waiting_state("LONG"), frame)
        self.assertEqual(state["last_processed_open_time"], BASE_OT)
        self.assertIsNone(candidate)


class ReflowLedgerTests(unittest.TestCase):
    def test_valid_json_non_objects_are_rejected_without_replacing_file(self):
        for raw in ("[]", "null", "1", '"ledger"'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ledger.json"
                path.write_text(raw, encoding="utf-8")

                with self.assertRaises(ValueError):
                    load_ledger(path)

                self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_round_trip_preserves_active_event_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger = {"version": 1, "symbols": {"TESTUSDT": make_waiting_state("LONG")}}
            save_ledger(path, ledger)
            self.assertEqual(load_ledger(path), ledger)

    def test_corrupt_file_is_not_replaced_with_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_ledger(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_failed_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            original = {"version": 1, "symbols": {}}
            save_ledger(path, original)
            with patch("momentum_reflow.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_ledger(path, {"version": 1, "symbols": {"X": {}}})
            self.assertEqual(load_ledger(path), original)


def make_closed_hourly_history(symbol):
    count = 100
    close = np.linspace(90.0, 100.0, count)
    return pd.DataFrame({
        "ot": (BASE_OT + HOUR_MS) - np.arange(count - 1, -1, -1) * HOUR_MS,
        "o": close - 0.05,
        "h": close + 0.10,
        "l": close - 0.10,
        "c": close,
        "v": np.full(count, 100.0),
    })


def make_closed_daily_history(symbol):
    count = 40
    close = np.full(count, 100.0)
    frame = pd.DataFrame({
        "ot": np.arange(count, dtype=np.int64) * 86_400_000,
        "o": close.copy(),
        "h": close + 1.0,
        "l": close - 1.0,
        "c": close.copy(),
        "v": np.full(count, 100.0),
    })
    frame.loc[frame.index[-1], ["o", "h", "l", "c", "v"]] = [
        100.0, 112.0, 99.0, 111.0, 300.0
    ]
    return frame


def make_return_window_hourly_history(symbol):
    hourly = make_closed_hourly_history(symbol)
    prior_ema = add_hourly_indicators(hourly.iloc[:-1]).iloc[-1]["ema50"]
    hourly.loc[hourly.index[-1], ["o", "h", "l", "c"]] = [
        prior_ema, prior_ema + 0.1, prior_ema - 0.1, prior_ema
    ]
    return hourly


class ReflowScanServiceTests(unittest.TestCase):
    @patch("momentum_reflow.daily_confirmation")
    @patch("momentum_reflow.advance_symbol")
    @patch("momentum_reflow.fetch_klines")
    def test_scan_symbol_returns_first_return_open_time(
        self, fetch, advance, confirmation
    ):
        return_open_time = BASE_OT + HOUR_MS
        fetch.side_effect = [
            make_closed_hourly_history("TESTUSDT"),
            make_closed_daily_history("TESTUSDT"),
        ]
        confirmation.return_value = {
            "passed": True,
            "kind": "strong_momentum",
            "rank": 3,
        }
        advance.return_value = (
            {
                "event": {
                    "breakout_open_time": BASE_OT,
                    "breakout_close_time": return_open_time,
                    "max_expansion_atr": 2.0,
                    "breakout_volume_ratio": 2.5,
                }
            },
            {
                "direction": "LONG",
                "return_open_time": return_open_time,
                "window_index": 1,
            },
        )

        _, candidate, _, _ = momentum_reflow._scan_symbol(
            "TESTUSDT", {}, False
        )

        self.assertEqual(candidate["return_open_time"], return_open_time)

    @patch("momentum_reflow.fetch_klines_range")
    def test_incremental_context_uses_full_ema_initialization_window(self, ranged):
        cursor = 2_000 * HOUR_MS
        history = candle_frame(1_000)
        history["ot"] = cursor - 998 * HOUR_MS + np.arange(len(history)) * HOUR_MS
        ranged.return_value = history

        momentum_reflow._scan_symbol(
            "TESTUSDT", {"last_processed_open_time": cursor, "event": None}, True
        )

        self.assertEqual(ranged.call_args.args[2], cursor - 1_000 * HOUR_MS)

    @patch("momentum_reflow.requests.get")
    def test_bitget_universe_keeps_crypto_commodity_and_fx_but_rejects_equity_and_unknown_rwa(self, get):
        contracts = Mock()
        contracts.raise_for_status.return_value = None
        contracts.json.return_value = {"data": [
            {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
            {"symbol": "XAUUSDT", "baseCoin": "XAU", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
            {"symbol": "EURUSDT", "baseCoin": "EUR", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
            {"symbol": "TSLAUSDT", "baseCoin": "TSLA", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
            {"symbol": "UNKNOWNUSDT", "baseCoin": "UNKNOWN", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
        ]}
        tickers = Mock()
        tickers.raise_for_status.return_value = None
        tickers.json.return_value = {"data": [
            {"symbol": symbol, "usdtVolume": "2000000"}
            for symbol in ("BTCUSDT", "XAUUSDT", "EURUSDT", "TSLAUSDT", "UNKNOWNUSDT")
        ]}
        get.side_effect = [contracts, tickers]
        symbols, _, types = fetch_futures_universe()
        self.assertEqual(symbols, ["BTCUSDT", "XAUUSDT", "EURUSDT"])
        self.assertEqual(types, {
            "BTCUSDT": "CRYPTO",
            "XAUUSDT": "COMMODITY",
            "EURUSDT": "FX",
        })
        self.assertIn("api.bitget.com/api/v2/mix/market/contracts", get.call_args_list[0].args[0])

    @patch("momentum_reflow.requests.get")
    def test_reflow_volume_boundary_is_inclusive(self, get):
        contracts = Mock()
        contracts.raise_for_status.return_value = None
        contracts.json.return_value = {"data": [{
            "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
            "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO",
        }]}
        tickers = Mock()
        tickers.raise_for_status.return_value = None
        tickers.json.return_value = {"data": [{"symbol": "BTCUSDT", "quoteVolume": "2000000"}]}
        get.side_effect = [contracts, tickers]

        symbols, _, _ = fetch_futures_universe()

        self.assertEqual(symbols, ["BTCUSDT"])

    @patch("momentum_reflow.requests.get")
    def test_reflow_universe_rejects_blocked_and_leveraged_contracts(self, get):
        contracts = Mock()
        contracts.raise_for_status.return_value = None
        contracts.json.return_value = {"data": [
            {"symbol": "USDCUSDT", "baseCoin": "USDC", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
            {"symbol": "BULLUSDT", "baseCoin": "BULL", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
            {"symbol": "BEARUSDT", "baseCoin": "BEAR", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
            {"symbol": "UPUSDT", "baseCoin": "UP", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
            {"symbol": "DOWNUSDT", "baseCoin": "DOWN", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
            {"symbol": "SUPERUSDT", "baseCoin": "SUPER", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
        ]}
        tickers = Mock()
        tickers.raise_for_status.return_value = None
        tickers.json.return_value = {"data": [
            {"symbol": row["symbol"], "quoteVolume": "2000000"}
            for row in contracts.json.return_value["data"]
        ]}
        get.side_effect = [contracts, tickers]

        symbols, _, types = fetch_futures_universe()

        self.assertEqual(symbols, ["SUPERUSDT"])
        self.assertEqual(types, {"SUPERUSDT": "CRYPTO"})

    def test_legacy_binance_ledger_resets_before_bitget_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {"version": 1, "symbols": {"BTCUSDT": make_waiting_state("LONG")}})
            ledger = momentum_reflow.prepare_bitget_ledger(load_ledger(path))
        self.assertEqual(ledger["source"], "bitget_usdt_futures")
        self.assertEqual(ledger["symbols"], {})

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_active_ledger_symbol_is_scanned_below_current_volume_filter(
        self, universe, latest, ranged
    ):
        universe.return_value = (["NEWUSDT"], {"NEWUSDT": 9_000_000.0}, {"NEWUSDT": "CRYPTO"})
        latest.side_effect = lambda symbol, interval, *args, **kwargs: (
            make_closed_hourly_history(symbol)
            if interval == "1h"
            else make_closed_daily_history(symbol)
        )
        ranged.side_effect = lambda symbol, *args, **kwargs: make_closed_hourly_history(symbol)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {
                "version": 1,
                "source": "bitget_usdt_futures",
                "symbols": {"OLDUSDT": make_waiting_state("LONG")},
            })
            payload = scan_momentum_reflow(path, max_workers=1)
        requested = {call.args[0] for call in latest.call_args_list + ranged.call_args_list}
        self.assertEqual(requested, {"NEWUSDT", "OLDUSDT"})
        self.assertEqual(payload["scanned"], 2)

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_gap_failure_preserves_symbol_state_and_cursor(self, universe, latest, ranged):
        universe.return_value = ([], {}, {})
        original = make_waiting_state("LONG")
        gapped = make_closed_hourly_history("TESTUSDT")
        gapped = gapped.drop(gapped.index[-2]).reset_index(drop=True)
        ranged.return_value = gapped
        latest.return_value = make_closed_daily_history("TESTUSDT")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {
                "version": 1,
                "source": "bitget_usdt_futures",
                "symbols": {"TESTUSDT": original},
            })
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(saved["symbols"]["TESTUSDT"], original)

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_leading_gap_after_cursor_preserves_symbol_state(self, universe, latest, ranged):
        universe.return_value = ([], {}, {})
        original = make_waiting_state("LONG")
        leading_gap = make_closed_hourly_history("TESTUSDT")
        leading_gap["ot"] = BASE_OT + 2 * HOUR_MS + np.arange(len(leading_gap)) * HOUR_MS
        ranged.return_value = leading_gap
        latest.return_value = make_closed_daily_history("TESTUSDT")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {"version": 1, "source": "bitget_usdt_futures", "symbols": {"TESTUSDT": original}})
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(saved["symbols"]["TESTUSDT"], original)

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_initial_and_incremental_reflow_reads_use_bitget_usdt_futures(
        self, universe, latest, ranged
    ):
        universe.return_value = (["NEWUSDT"], {"NEWUSDT": 9_000_000.0}, {"NEWUSDT": "CRYPTO"})
        latest.side_effect = lambda symbol, interval, *args, **kwargs: (
            make_closed_hourly_history(symbol)
            if interval == "1h"
            else make_closed_daily_history(symbol)
        )
        ranged.return_value = make_return_window_hourly_history("OLDUSDT")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {
                "version": 1,
                "source": "bitget_usdt_futures",
                "symbols": {"OLDUSDT": make_waiting_state("LONG")},
            })
            scan_momentum_reflow(path, max_workers=1)
        latest.assert_any_call(
            "NEWUSDT", "1h", 1000, exchange="bitget", closed_only=True,
            market_type="futures", testnet=False,
        )
        latest.assert_any_call(
            "OLDUSDT", "1d", 40, exchange="bitget", closed_only=True,
            market_type="futures", testnet=False,
        )
        ranged.assert_called_once_with(
            "OLDUSDT", "1h", 0, exchange="bitget",
            market_type="futures", testnet=False,
        )

    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_low_price_new_symbol_is_not_initialized(self, universe, latest):
        universe.return_value = (["LOWUSDT"], {"LOWUSDT": 9_000_000.0}, {"LOWUSDT": "CRYPTO"})
        low_price = make_closed_hourly_history("LOWUSDT")
        low_price.loc[:, ["o", "h", "l", "c"]] = 0.0005
        latest.return_value = low_price
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["initialized"], 0)
        self.assertEqual(saved["symbols"], {})
        self.assertEqual(latest.call_count, 1)

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_no_candidate_advances_state_without_daily_data(self, universe, latest, ranged):
        universe.return_value = ([], {}, {})
        ranged.return_value = make_closed_hourly_history("TESTUSDT")
        latest.side_effect = AssertionError("daily fetch must not run")
        original = make_waiting_state("LONG")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {"version": 1, "source": "bitget_usdt_futures", "symbols": {"TESTUSDT": original}})
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["errors"], 0)
        self.assertEqual(saved["symbols"]["TESTUSDT"]["last_processed_open_time"], BASE_OT + HOUR_MS)
        latest.assert_not_called()

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_daily_outage_preserves_exact_old_state(self, universe, latest, ranged):
        universe.return_value = ([], {}, {})
        ranged.return_value = make_return_window_hourly_history("TESTUSDT")
        latest.side_effect = OSError("daily unavailable")
        original = make_waiting_state("LONG")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {
                "version": 1,
                "source": "bitget_usdt_futures",
                "symbols": {"TESTUSDT": original},
            })
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(payload["rows"], [])
        self.assertEqual(saved["symbols"]["TESTUSDT"], original)

    @patch("momentum_reflow.advance_symbol")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_new_symbol_daily_outage_is_not_initialized_or_persisted(
        self, universe, latest, advance
    ):
        universe.return_value = (["NEWUSDT"], {"NEWUSDT": 9_000_000.0}, {"NEWUSDT": "CRYPTO"})
        latest.side_effect = [make_closed_hourly_history("NEWUSDT"), OSError("daily unavailable")]
        advance.return_value = (
            {"last_processed_open_time": BASE_OT + HOUR_MS, "event": {}},
            {"direction": "LONG", "return_open_time": BASE_OT + HOUR_MS},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(payload["initialized"], 0)
        self.assertEqual(saved["symbols"], {})

    @patch("momentum_reflow.save_ledger", wraps=save_ledger)
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_progress_reports_each_worker_and_ledger_saves_once(
        self, universe, latest, saved
    ):
        universe.return_value = (["AAAUSDT", "BBBUSDT"], {}, {"AAAUSDT": "CRYPTO", "BBBUSDT": "CRYPTO"})
        latest.return_value = make_closed_hourly_history("ANYUSDT")
        progress = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            scan_momentum_reflow(path, progress=progress, max_workers=1)
        self.assertEqual(progress.call_args_list, [((1, 2),), ((2, 2),)])
        saved.assert_called_once()

    @patch("momentum_reflow._scan_symbol")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_rows_sort_by_required_keys_and_truncate_to_eighty(self, universe, worker):
        symbols = ["AUSDT", "BUSDT", "CUSDT"] + [f"X{index:02d}USDT" for index in range(78)]
        universe.return_value = (symbols, {}, {"AUSDT": "COMMODITY", "BUSDT": "FX"})
        candidates = {
            "AUSDT": {"close_distance_atr": 1.0, "daily_rank": 1, "breakout_volume_ratio": 9.0},
            "BUSDT": {"close_distance_atr": 1.0, "daily_rank": 2, "breakout_volume_ratio": 1.0},
            "CUSDT": {"close_distance_atr": 1.0, "daily_rank": 2, "breakout_volume_ratio": 2.0},
        }
        for index in range(78):
            candidates[f"X{index:02d}USDT"] = {
                "close_distance_atr": float(index + 2),
                "daily_rank": 1,
                "breakout_volume_ratio": 1.0,
            }

        def result(symbol, old_state, existing):
            return {}, {"symbol": symbol, **candidates[symbol]}, False, False

        worker.side_effect = result
        with tempfile.TemporaryDirectory() as directory:
            payload = scan_momentum_reflow(Path(directory) / "ledger.json", max_workers=1)
        self.assertEqual([row["symbol"] for row in payload["rows"][:3]], ["CUSDT", "BUSDT", "AUSDT"])
        self.assertEqual(
            {row["symbol"]: row["instrument_type"] for row in payload["rows"][:3]},
            {"CUSDT": "CRYPTO", "BUSDT": "FX", "AUSDT": "COMMODITY"},
        )
        self.assertEqual(len(payload["rows"]), 80)
        self.assertEqual(payload["rows"][-1]["symbol"], "X76USDT")

    @patch("momentum_reflow.fetch_klines_range")
    @patch("momentum_reflow.fetch_klines")
    @patch("momentum_reflow.fetch_futures_universe")
    def test_failed_daily_confirmation_keeps_active_return_window(self, universe, latest, ranged):
        universe.return_value = ([], {}, {})
        hourly = make_return_window_hourly_history("TESTUSDT")
        daily = make_closed_daily_history("TESTUSDT")
        daily.loc[daily.index[-1], ["o", "h", "l", "c", "v"]] = [100, 101, 99, 100, 100]
        ranged.return_value = hourly
        latest.return_value = daily
        active = make_waiting_state("LONG")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            save_ledger(path, {"version": 1, "source": "bitget_usdt_futures", "symbols": {"TESTUSDT": active}})
            payload = scan_momentum_reflow(path, max_workers=1)
            saved = load_ledger(path)
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["errors"], 0)
        self.assertEqual(saved["symbols"]["TESTUSDT"]["event"]["state"], "RETURN_WINDOW")


if __name__ == "__main__":
    unittest.main()
