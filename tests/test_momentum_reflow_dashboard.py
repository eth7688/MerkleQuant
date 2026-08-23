import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from momentum_reflow_dashboard import (
    beijing_day,
    load_reflow_settings,
    load_reflow_dashboard,
    merge_reflow_signals,
    next_reflow_scan_at,
    save_reflow_settings,
    score_reflow_candidate,
)


class ReflowSettingsTests(unittest.TestCase):
    def test_missing_settings_default_to_enabled(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"

            settings = load_reflow_settings(path)

            self.assertTrue(settings["auto_scan_enabled"])
            self.assertEqual(settings["version"], 1)
            self.assertTrue(path.exists())

    def test_corrupt_settings_fail_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaises(ValueError):
                load_reflow_settings(path)

            self.assertEqual(path.read_bytes(), original)

    def test_save_requires_real_boolean(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"

            with self.assertRaises(TypeError):
                save_reflow_settings(path, 1, "admin", 123)

    def test_save_rejects_corrupt_existing_settings_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaises(ValueError):
                save_reflow_settings(path, False, "admin", 123)

            self.assertEqual(path.read_bytes(), original)

    def test_failed_atomic_replace_preserves_old_settings(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = (
                b'{"version":1,"auto_scan_enabled":true,'
                b'"updated_at":100,"updated_by":"admin"}'
            )
            path.write_bytes(original)

            with patch("momentum_reflow_dashboard.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    save_reflow_settings(path, False, "admin", 123)

            self.assertEqual(path.read_bytes(), original)


class ReflowQualityScoreTests(unittest.TestCase):
    def test_high_quality_score_uses_confirmed_caps(self):
        row = {
            "daily_rank": 3,
            "breakout_volume_ratio": 3.5,
            "max_expansion_atr": 4.0,
            "close_distance_atr": 0.0,
            "window_index": 1,
        }

        scored = score_reflow_candidate(row)

        self.assertEqual(scored["quality_score"], 100)
        self.assertEqual(scored["quality_label"], "HIGH")

    def test_score_boundaries_are_stable(self):
        def row_for(total, daily_rank, window_index):
            daily_points = {3: 30, 2: 24, 1: 18}
            window_points = {1: 10, 2: 8, 3: 6, 4: 4, 5: 2}
            fixed = daily_points[daily_rank] + 15 + 12 + window_points[window_index]
            distance_points = total - fixed
            return {
                "daily_rank": daily_rank,
                "breakout_volume_ratio": 1.5,
                "max_expansion_atr": 1.5,
                "close_distance_atr": (15 - distance_points) * 0.35 / 15,
                "window_index": window_index,
            }

        cases = [
            (75, 2, 1, "HIGH"),
            (74, 2, 1, "STANDARD"),
            (60, 1, 5, "STANDARD"),
            (59, 1, 5, "WATCH"),
        ]
        for total, daily_rank, window_index, label in cases:
            with self.subTest(total=total):
                scored = score_reflow_candidate(
                    row_for(total, daily_rank, window_index)
                )
                self.assertEqual(scored["quality_score"], total)
                self.assertEqual(scored["quality_label"], label)

        components = score_reflow_candidate(row_for(74, 2, 1))["score_components"]
        self.assertEqual(
            components,
            {
                "daily": 24.0,
                "volume": 15.0,
                "expansion": 12.0,
                "distance": 13.0,
                "window": 10.0,
            },
        )

    def test_invalid_or_nonfinite_required_values_are_rejected(self):
        valid = {
            "daily_rank": 3,
            "breakout_volume_ratio": 3.0,
            "max_expansion_atr": 3.0,
            "close_distance_atr": 0.0,
            "window_index": 1,
        }
        for field, value in (("daily_rank", 4), ("window_index", 0),
                             ("max_expansion_atr", float("nan")),
                             ("close_distance_atr", float("inf"))):
            row = valid.copy()
            row[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    score_reflow_candidate(row)

        with self.assertRaises(ValueError):
            score_reflow_candidate({})

    def test_extremely_large_integer_is_rejected_as_invalid_value(self):
        row = {
            "daily_rank": 3,
            "breakout_volume_ratio": 10 ** 10_000,
            "max_expansion_atr": 3.0,
            "close_distance_atr": 0.0,
            "window_index": 1,
        }

        with self.assertRaises(ValueError):
            score_reflow_candidate(row)

    def test_boolean_required_numeric_values_are_rejected(self):
        valid = {
            "daily_rank": 3,
            "breakout_volume_ratio": 3.0,
            "max_expansion_atr": 3.0,
            "close_distance_atr": 0.0,
            "window_index": 1,
        }
        for field in valid:
            with self.subTest(field=field):
                row = valid.copy()
                row[field] = True

                with self.assertRaises(ValueError):
                    score_reflow_candidate(row)


def milliseconds(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def candidate(**overrides) -> dict:
    row = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "instrument_type": "CRYPTO",
        "breakout_time": 100,
        "breakout_close_time": 3_600_100,
        "return_open_time": 1_000,
        "price": 100.0,
        "ema50": 99.0,
        "window_index": 1,
        "daily_kind": "strong_momentum",
        "daily_rank": 3,
        "breakout_volume_ratio": 3.0,
        "max_expansion_atr": 3.0,
        "close_distance_atr": 0.0,
    }
    row.update(overrides)
    return row


def write_ledger(path: Path, event: dict | None = None) -> None:
    symbols = {} if event is None else {"BTCUSDT": {"event": event}}
    path.write_text(
        json.dumps({"version": 1, "symbols": symbols}),
        encoding="utf-8",
    )


class ReflowDailyHistoryTests(unittest.TestCase):
    def test_same_signal_upserts_and_newer_signal_sorts_first(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            write_ledger(ledger)
            first = candidate(return_open_time=1_000, window_index=1)
            update = candidate(return_open_time=1_000, window_index=2)
            newer = candidate(return_open_time=2_000, window_index=1)

            merge_reflow_signals(history, ledger, {"rows": [first]}, 1_000)
            merge_reflow_signals(history, ledger, {"rows": [update, newer]}, 1_001)

            rows = load_reflow_dashboard(history, 1_001)["rows"]
            self.assertEqual([row["return_open_time"] for row in rows], [2_000, 1_000])
            self.assertEqual(rows[1]["window_index"], 2)
            self.assertEqual(rows[1]["first_seen_at"], 1_000)
            self.assertEqual(rows[1]["last_seen_at"], 1_001)

    def test_beijing_midnight_separates_days(self):
        before = milliseconds("2026-07-30T15:59:59Z")
        after = milliseconds("2026-07-30T16:00:00Z")

        self.assertNotEqual(beijing_day(before), beijing_day(after))

    def test_terminal_ledger_updates_status_without_deleting_signal(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            row = candidate()
            active_event = {
                "direction": "LONG",
                "breakout_open_time": 100,
                "return_open_time": 1_000,
                "state": "RETURN_WINDOW",
                "audit_reason": "return_window_open",
            }
            write_ledger(ledger, active_event)
            merge_reflow_signals(history, ledger, {"rows": [row]}, 1_000)
            active_event.update(
                state="CONSUMED", audit_reason="return_window_complete"
            )
            write_ledger(ledger, active_event)

            merge_reflow_signals(history, ledger, {"rows": []}, 1_001)

            rows = load_reflow_dashboard(history, 1_001)["rows"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "WINDOW_COMPLETE")
            self.assertEqual(rows[0]["status_reason"], "return_window_complete")

    def test_terminal_ledger_updates_earlier_return_window_for_same_breakout(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            event = {
                "direction": "LONG",
                "breakout_open_time": 100,
                "return_open_time": 1_000,
                "state": "RETURN_WINDOW",
                "audit_reason": "return_window_open",
            }
            write_ledger(ledger, event)
            merge_reflow_signals(
                history, ledger, {"rows": [candidate(return_open_time=1_000)]}, 1_000
            )
            event.update(
                return_open_time=2_000,
                state="CONSUMED",
                audit_reason="return_window_complete",
            )
            write_ledger(ledger, event)

            merge_reflow_signals(history, ledger, {"rows": []}, 2_001)

            rows = load_reflow_dashboard(history, 2_001)["rows"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["return_open_time"], 1_000)
            self.assertEqual(rows[0]["status"], "WINDOW_COMPLETE")
            self.assertEqual(rows[0]["status_reason"], "return_window_complete")

    def test_active_ledger_does_not_relabel_earlier_return_window(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            event = {
                "direction": "LONG",
                "breakout_open_time": 100,
                "return_open_time": 1_000,
                "state": "RETURN_WINDOW",
                "audit_reason": "return_window_open",
            }
            write_ledger(ledger, event)
            merge_reflow_signals(
                history, ledger, {"rows": [candidate(return_open_time=1_000)]}, 1_000
            )
            event.update(
                return_open_time=2_000,
                audit_reason="return_window_second_candle",
            )
            write_ledger(ledger, event)

            merge_reflow_signals(history, ledger, {"rows": []}, 2_001)

            stored = load_reflow_dashboard(history, 2_001)["rows"][0]
            self.assertEqual(stored["status"], "ACTIVE")
            self.assertEqual(stored["status_reason"], "return_window_open")

    def test_invalid_terminal_state_and_freshness_fields_are_persisted(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            row = candidate(return_open_time=2_000)
            event = {
                "direction": "LONG",
                "breakout_open_time": 100,
                "return_open_time": 2_000,
                "state": "INVALIDATED",
                "audit_reason": "ema_slope_reversal",
            }
            write_ledger(ledger, event)

            merge_reflow_signals(history, ledger, {"rows": [row]}, 3_000)

            stored = load_reflow_dashboard(history, 3_000)["rows"][0]
            self.assertEqual(stored["status"], "INVALID")
            self.assertEqual(stored["return_close_time"], 3_602_000)
            self.assertEqual(stored["first_seen_at"], 3_000)
            self.assertEqual(stored["last_seen_at"], 3_000)

    def test_sort_ties_are_stable_and_previous_days_stay_on_disk(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            write_ledger(ledger)
            day_one = milliseconds("2026-07-30T15:59:59Z")
            day_two = milliseconds("2026-07-30T16:00:00Z")
            alpha = candidate(symbol="ALPHAUSDT", return_open_time=1_000)
            beta = candidate(symbol="BETAUSDT", return_open_time=1_000)

            merge_reflow_signals(history, ledger, {"rows": [beta, alpha]}, day_one)
            self.assertEqual(
                [row["symbol"] for row in load_reflow_dashboard(history, day_one)["rows"]],
                ["ALPHAUSDT", "BETAUSDT"],
            )
            merge_reflow_signals(history, ledger, {"rows": []}, day_two)

            self.assertEqual(load_reflow_dashboard(history, day_two)["rows"], [])
            raw = json.loads(history.read_text(encoding="utf-8"))
            self.assertEqual(len(raw["days"]), 2)

    def test_corrupt_history_is_preserved_without_rewrite(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            history.write_text("{broken", encoding="utf-8")
            original = history.read_bytes()
            write_ledger(ledger)

            with self.assertRaises(ValueError):
                merge_reflow_signals(history, ledger, {"rows": [candidate()]}, 1_000)

            self.assertEqual(history.read_bytes(), original)

    def test_incomplete_persisted_signal_is_rejected_without_rewrite(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            write_ledger(ledger)
            merge_reflow_signals(history, ledger, {"rows": [candidate()]}, 1_000)
            raw = json.loads(history.read_text(encoding="utf-8"))
            stored = next(iter(raw["days"].values()))["signals"]
            next(iter(stored.values())).pop("price")
            history.write_text(json.dumps(raw), encoding="utf-8")
            original = history.read_bytes()

            with self.assertRaises(ValueError):
                load_reflow_dashboard(history, 1_000)

            self.assertEqual(history.read_bytes(), original)

    def test_invalid_persisted_enums_and_types_are_rejected_without_rewrite(self):
        invalid_values = (
            ("direction", "SIDEWAYS"),
            ("instrument_type", "BOND"),
            ("quality_label", "BEST"),
            ("status", "PENDING"),
            ("price", "100.0"),
            ("score_components", []),
        )
        for field, value in invalid_values:
            with self.subTest(field=field), TemporaryDirectory() as folder:
                history = Path(folder) / "history.json"
                ledger = Path(folder) / "ledger.json"
                write_ledger(ledger)
                merge_reflow_signals(history, ledger, {"rows": [candidate()]}, 1_000)
                raw = json.loads(history.read_text(encoding="utf-8"))
                stored = next(iter(raw["days"].values()))["signals"]
                next(iter(stored.values()))[field] = value
                history.write_text(json.dumps(raw), encoding="utf-8")
                original = history.read_bytes()

                with self.assertRaises(ValueError):
                    load_reflow_dashboard(history, 1_000)

                self.assertEqual(history.read_bytes(), original)

    def test_mismatched_outer_signal_key_rejects_load_and_merge_without_rewrite(self):
        with TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            ledger = Path(folder) / "ledger.json"
            write_ledger(ledger)
            row = candidate()
            merge_reflow_signals(history, ledger, {"rows": [row]}, 1_000)
            raw = json.loads(history.read_text(encoding="utf-8"))
            signals = next(iter(raw["days"].values()))["signals"]
            signal = next(iter(signals.values()))
            signals["WRONG|KEY|0|0"] = signal
            del signals[signal["signal_key"]]
            history.write_text(json.dumps(raw), encoding="utf-8")
            original = history.read_bytes()

            with self.assertRaises(ValueError):
                load_reflow_dashboard(history, 1_000)
            self.assertEqual(history.read_bytes(), original)

            with self.assertRaises(ValueError):
                merge_reflow_signals(history, ledger, {"rows": [row]}, 1_001)
            self.assertEqual(history.read_bytes(), original)

    def test_next_scan_targets_the_next_beijing_hour_at_minute_three(self):
        before = datetime(2026, 7, 30, 10, 2, tzinfo=timezone.utc)
        at_target = datetime(2026, 7, 30, 10, 3, tzinfo=timezone.utc)

        self.assertEqual(next_reflow_scan_at(before).minute, 3)
        self.assertEqual(next_reflow_scan_at(before).hour, 18)
        self.assertEqual(next_reflow_scan_at(at_target).hour, 19)

        with self.assertRaises(ValueError):
            next_reflow_scan_at(datetime(2026, 7, 30, 10, 2))

if __name__ == "__main__":
    unittest.main()
