import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import tools.backfill_demo_daily_patterns as backfill_tool
from tools.backfill_demo_daily_patterns import (
    backfill_records, build_entry_time_index, parse_time_ms,
)


DAY_MS = 86_400_000
CUTOFF = 1_785_571_832_000
BEIJING_OFFSET_MS = 8 * 60 * 60 * 1_000


def stored_project_iso(actual_ms):
    return datetime.fromtimestamp(
        (actual_ms + BEIJING_OFFSET_MS) / 1000,
        timezone.utc,
    ).isoformat()


def frame_with_future_pattern():
    return pd.DataFrame({
        "ot": [CUTOFF - 3 * DAY_MS, CUTOFF - 2 * DAY_MS,
               CUTOFF - DAY_MS, CUTOFF + DAY_MS],
        "o": [100.0, 101.0, 98.5, 102.0],
        "h": [101.0, 102.0, 102.0, 103.0],
        "l": [99.0, 98.0, 98.0, 97.0],
        "c": [100.0, 99.0, 101.5, 98.0],
        "v": [100.0] * 4,
    })


def missing(symbol="TESTUSDT", entry_ms=CUTOFF + 1_000):
    return {
        "symbol": symbol,
        "direction": "LONG",
        "entry_time": stored_project_iso(entry_ms),
        "entry_price": 100.0,
        "daily_pattern": {"recorded": False, "reason": "not_recorded"},
    }


def closed_trade(signal_key="SIG-1", exit_ms=CUTOFF + DAY_MS):
    row = missing(entry_ms=CUTOFF + 1_000)
    row.pop("entry_time")
    row["time"] = stored_project_iso(exit_ms)
    row["signal_key"] = signal_key
    return row


class DailyPatternBackfillTest(unittest.TestCase):
    def test_documented_cli_entry_point_loads_project_imports(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "tools" / "backfill_demo_daily_patterns.py"), "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parse_time_accepts_iso_and_milliseconds(self):
        self.assertEqual(parse_time_ms(CUTOFF), CUTOFF)
        self.assertEqual(parse_time_ms("2026-08-01T08:10:32+00:00"), CUTOFF)

    def test_stored_project_time_converts_beijing_wall_clock_at_cutoff(self):
        parse_stored = getattr(backfill_tool, "parse_stored_project_time_ms", None)
        self.assertTrue(callable(parse_stored), "stored-project timestamp parser is required")
        self.assertEqual(
            parse_stored("2026-08-01T16:10:31+00:00"),
            CUTOFF - 1_000,
        )
        self.assertEqual(
            parse_stored("2026-08-01T16:10:33+00:00"),
            CUTOFF + 1_000,
        )

    def test_position_cutoff_uses_converted_stored_entry_time(self):
        before = missing(symbol="BEFOREUSDT", entry_ms=CUTOFF - 1_000)
        after = missing(symbol="AFTERUSDT", entry_ms=CUTOFF + 1_000)

        positions, _, report = backfill_records(
            [before, after], [], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )

        self.assertFalse(positions[0]["daily_pattern"]["recorded"])
        self.assertTrue(positions[1]["daily_pattern"]["recorded"])
        self.assertEqual(report["targets"], 1)
        self.assertEqual(report["skipped_pre_cutoff"], 1)

    def test_entry_index_rejects_same_time_duplicate(self):
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        with self.assertRaises(RuntimeError):
            build_entry_time_index([event, event])

    def test_entry_index_rejects_valid_and_invalid_duplicate(self):
        valid = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        invalid = json.dumps({
            "event": "entry_filled",
            "time": "invalid",
            "signal_key": "SIG-1",
        })
        with self.assertRaises(RuntimeError):
            build_entry_time_index([valid, invalid])

    def test_backfill_uses_entry_time_and_ignores_future_candle(self):
        position = missing()
        positions, lines, report = backfill_records(
            [position], [], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(report["updated_positions"], 1)
        self.assertEqual(positions[0]["daily_pattern"]["kind"], "bullish_engulfing")
        self.assertLessEqual(
            positions[0]["daily_pattern"]["candle_close_time"],
            CUTOFF + 1_000,
        )

    def test_pre_cutoff_and_recorded_rows_are_unchanged(self):
        old = missing(entry_ms=CUTOFF - 1)
        recorded = missing(symbol="DONEUSDT")
        recorded["daily_pattern"] = {"recorded": True, "kind": "hammer"}
        original = copy.deepcopy([old, recorded])
        positions, _, report = backfill_records(
            [old, recorded], [], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(positions, original)
        self.assertEqual(report["targets"], 0)
        self.assertEqual(report["skipped_pre_cutoff"], 1)
        self.assertEqual(report["skipped_recorded"], 1)

    def test_trade_jsonl_preserves_untouched_lines_and_is_idempotent(self):
        untouched = json.dumps(missing(entry_ms=CUTOFF - 1), ensure_ascii=False)
        target = json.dumps(closed_trade(), ensure_ascii=False)
        positions, lines, first = backfill_records(
            [], [untouched, target], {"SIG-1": CUTOFF + 1_000},
            CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(lines[0], untouched)
        _, second_lines, second = backfill_records(
            positions, lines, {"SIG-1": CUTOFF + 1_000},
            CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(second_lines, lines)
        self.assertEqual(second["targets"], 0)

    def test_trade_jsonl_preserves_blank_lines(self):
        target = json.dumps(closed_trade(), ensure_ascii=False)
        _, lines, _ = backfill_records(
            [], ["", target], {"SIG-1": CUTOFF + 1_000},
            CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(lines[0], "")
        self.assertEqual(json.loads(lines[1])["daily_pattern"]["recorded"], True)

    def test_cli_apply_preserves_untouched_crlf_bytes(self):
        untouched = json.dumps(missing(entry_ms=CUTOFF - 1), ensure_ascii=False)
        target = json.dumps(closed_trade(), ensure_ascii=False)
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "positions_<uid>.json").write_bytes(b"[]\r\n")
            trades_path = root / "trades_<uid>.jsonl"
            trades_path.write_bytes(
                f"{untouched}\r\n{target}\r\n".encode("utf-8")
            )
            (root / "signal_events_0.jsonl").write_bytes(
                f"{event}\r\n".encode("utf-8")
            )

            argv = [
                "backfill_demo_daily_patterns.py",
                "--root", str(root),
                "--apply",
            ]
            with (
                patch.object(backfill_tool, "load_history", return_value=frame_with_future_pattern()),
                patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                result = backfill_tool.main()

            self.assertEqual(result, 0)

            migrated = trades_path.read_bytes()
            self.assertTrue(
                migrated.startswith(f"{untouched}\r\n".encode("utf-8"))
            )
            self.assertEqual(migrated.count(b"\r\n"), 2)
            self.assertNotIn(b"\n", migrated.replace(b"\r\n", b""))
            changed = migrated.split(b"\r\n")[1]
            self.assertTrue(json.loads(changed)["daily_pattern"]["recorded"])

    def test_cli_dry_run_prints_report_without_writing_outputs(self):
        target = json.dumps(closed_trade(), ensure_ascii=False)
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            positions_path = root / "positions_<uid>.json"
            trades_path = root / "trades_<uid>.jsonl"
            positions_path.write_bytes(b"[]\r\n")
            trades_path.write_bytes(f"{target}\r\n".encode("utf-8"))
            (root / "signal_events_0.jsonl").write_bytes(
                f"{event}\r\n".encode("utf-8")
            )
            original_positions = positions_path.read_bytes()
            original_trades = trades_path.read_bytes()
            output = io.StringIO()

            argv = ["backfill_demo_daily_patterns.py", "--root", str(root)]
            with (
                patch.object(backfill_tool, "load_history", return_value=frame_with_future_pattern()),
                patch.object(sys, "argv", argv),
                redirect_stdout(output),
            ):
                result = backfill_tool.main()

            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(report["targets"], 1)
            self.assertEqual(report["updated_trades"], 1)
            self.assertEqual(positions_path.read_bytes(), original_positions)
            self.assertEqual(trades_path.read_bytes(), original_trades)

    def test_cli_apply_second_stage_failure_preserves_both_outputs(self):
        target = json.dumps(closed_trade(), ensure_ascii=False)
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            positions_path = root / "positions_<uid>.json"
            trades_path = root / "trades_<uid>.jsonl"
            positions_path.write_bytes(b"[]\r\n")
            trades_path.write_bytes(f"{target}\r\n".encode("utf-8"))
            (root / "signal_events_0.jsonl").write_bytes(
                f"{event}\r\n".encode("utf-8")
            )
            original_positions = positions_path.read_bytes()
            original_trades = trades_path.read_bytes()
            real_atomic_write = backfill_tool.atomic_write
            call_count = 0

            def fail_second_stage(path, text):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("second stage failed")
                return real_atomic_write(path, text)

            argv = [
                "backfill_demo_daily_patterns.py",
                "--root", str(root),
                "--apply",
            ]
            output = io.StringIO()
            with (
                patch.object(backfill_tool, "load_history", return_value=frame_with_future_pattern()),
                patch.object(backfill_tool, "atomic_write", side_effect=fail_second_stage),
                patch.object(sys, "argv", argv),
                redirect_stdout(output),
            ):
                result = backfill_tool.main()

            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertEqual(report["failures"], 1)
            self.assertIn("second stage failed", report["error"])
            self.assertEqual(positions_path.read_bytes(), original_positions)
            self.assertEqual(trades_path.read_bytes(), original_trades)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_cli_failure_report_is_bounded_redacted_and_preserves_outputs(self):
        target = json.dumps(closed_trade(), ensure_ascii=False)
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            positions_path = root / "positions_<uid>.json"
            trades_path = root / "trades_<uid>.jsonl"
            positions_path.write_bytes(b"[]\n")
            trades_path.write_bytes(f"{target}\n".encode("utf-8"))
            (root / "signal_events_0.jsonl").write_text(event + "\n", encoding="utf-8")
            original_positions = positions_path.read_bytes()
            original_trades = trades_path.read_bytes()
            output = io.StringIO()
            diagnostic = "fetch failed api_key=do-not-print " + ("x" * 1_000)

            argv = ["backfill_demo_daily_patterns.py", "--root", str(root)]
            with (
                patch.object(backfill_tool, "load_history", side_effect=RuntimeError(diagnostic)),
                patch.object(sys, "argv", argv),
                redirect_stdout(output),
            ):
                result = backfill_tool.main()

            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertEqual(report["failures"], 1)
            self.assertIn("RuntimeError", report["error"])
            self.assertNotIn("do-not-print", report["error"])
            self.assertLessEqual(len(report["error"]), 240)
            self.assertEqual(positions_path.read_bytes(), original_positions)
            self.assertEqual(trades_path.read_bytes(), original_trades)

    def test_cli_second_final_replace_failure_rolls_back_first_output(self):
        position = missing(symbol="POSITIONUSDT")
        target = json.dumps(closed_trade(), ensure_ascii=False)
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            positions_path = root / "positions_<uid>.json"
            trades_path = root / "trades_<uid>.jsonl"
            positions_path.write_text(json.dumps([position]) + "\n", encoding="utf-8")
            trades_path.write_text(target + "\n", encoding="utf-8")
            (root / "signal_events_0.jsonl").write_text(event + "\n", encoding="utf-8")
            original_positions = positions_path.read_bytes()
            original_trades = trades_path.read_bytes()
            real_replace = backfill_tool.os.replace
            replace_calls = 0

            def fail_second_replace(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("second final replace failed")
                return real_replace(source, destination)

            output = io.StringIO()
            argv = [
                "backfill_demo_daily_patterns.py",
                "--root", str(root),
                "--apply",
            ]
            with (
                patch.object(backfill_tool, "load_history", return_value=frame_with_future_pattern()),
                patch.object(backfill_tool.os, "replace", side_effect=fail_second_replace),
                patch.object(sys, "argv", argv),
                redirect_stdout(output),
            ):
                result = backfill_tool.main()

            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertEqual(report["failures"], 1)
            self.assertEqual(positions_path.read_bytes(), original_positions)
            self.assertEqual(trades_path.read_bytes(), original_trades)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_any_loader_failure_raises_before_mutating_inputs(self):
        positions = [missing()]
        original = copy.deepcopy(positions)
        with self.assertRaises(RuntimeError):
            backfill_records(
                positions, [], {}, CUTOFF,
                lambda _: (_ for _ in ()).throw(RuntimeError("network")),
            )
        self.assertEqual(positions, original)

    def test_trade_uses_entry_event_not_exit_time(self):
        event = json.dumps({
            "event": "entry_filled", "time": "2026-08-01T16:10:33+00:00",
            "signal_key": "SIG-1",
        })
        index = build_entry_time_index([event])
        trade = json.dumps(closed_trade(exit_ms=CUTOFF + 10 * DAY_MS))
        _, lines, report = backfill_records(
            [], [trade], index, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        migrated = json.loads(lines[0])
        self.assertEqual(index["SIG-1"], CUTOFF + 1_000)
        self.assertLessEqual(
            migrated["daily_pattern"]["candle_close_time"], index["SIG-1"],
        )

    def test_entry_event_excludes_utc_midnight_candle_after_actual_fill(self):
        actual_fill = int(
            datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc).timestamp() * 1_000
        )
        midnight_after_fill = int(
            datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1_000
        )
        event = json.dumps({
            "event": "entry_filled",
            "time": "2026-08-02T04:00:00+00:00",
            "signal_key": "SIG-MIDNIGHT",
        })
        index = build_entry_time_index([event])
        frame = pd.DataFrame({
            "ot": [midnight_after_fill - 2 * DAY_MS,
                   midnight_after_fill - DAY_MS,
                   midnight_after_fill],
            "o": [100.0, 100.0, 100.0],
            "h": [101.0, 101.0, 101.0],
            "l": [99.0, 99.0, 99.0],
            "c": [100.0, 100.0, 100.0],
            "v": [100.0, 100.0, 100.0],
        })
        trade = json.dumps(closed_trade(signal_key="SIG-MIDNIGHT"))

        _, lines, _ = backfill_records(
            [], [trade], index, CUTOFF, lambda _: frame,
        )

        migrated = json.loads(lines[0])
        self.assertEqual(index["SIG-MIDNIGHT"], actual_fill)
        self.assertEqual(
            migrated["daily_pattern"]["candle_close_time"],
            midnight_after_fill - DAY_MS,
        )

    def test_trade_exit_fail_closed_check_uses_converted_stored_time(self):
        before = json.dumps(closed_trade(
            signal_key="MISSING-BEFORE", exit_ms=CUTOFF - 1_000,
        ))
        _, lines, report = backfill_records(
            [], [before], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(lines, [before])
        self.assertEqual(report["targets"], 0)

        after = json.dumps(closed_trade(
            signal_key="MISSING-AFTER", exit_ms=CUTOFF + 1_000,
        ))
        with self.assertRaises(RuntimeError):
            backfill_records(
                [], [after], {}, CUTOFF, lambda _: frame_with_future_pattern(),
            )

    def test_recent_trade_without_unique_entry_event_aborts(self):
        trade = json.dumps(closed_trade(signal_key="MISSING"))
        with self.assertRaises(RuntimeError):
            backfill_records(
                [], [trade], {}, CUTOFF, lambda _: frame_with_future_pattern(),
            )

    def test_recent_recorded_trade_skips_missing_entry_event(self):
        recorded = closed_trade(signal_key="MISSING")
        recorded["daily_pattern"] = {"recorded": True, "kind": "hammer"}
        raw = json.dumps(recorded, ensure_ascii=False)
        _, lines, report = backfill_records(
            [], [raw], {}, CUTOFF,
            lambda _: (_ for _ in ()).throw(AssertionError("loader called")),
        )
        self.assertEqual(lines, [raw])
        self.assertEqual(report["targets"], 0)
        self.assertEqual(report["skipped_recorded"], 1)
