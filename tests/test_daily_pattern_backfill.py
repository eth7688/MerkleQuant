import copy
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tools.backfill_demo_daily_patterns import (
    backfill_records, build_entry_time_index, parse_time_ms,
)


DAY_MS = 86_400_000
CUTOFF = 1_785_571_832_000


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
        "entry_time": datetime.fromtimestamp(entry_ms / 1000, timezone.utc).isoformat(),
        "entry_price": 100.0,
        "daily_pattern": {"recorded": False, "reason": "not_recorded"},
    }


def closed_trade(signal_key="SIG-1", exit_ms=CUTOFF + DAY_MS):
    row = missing(entry_ms=CUTOFF + 1_000)
    row.pop("entry_time")
    row["time"] = datetime.fromtimestamp(exit_ms / 1000, timezone.utc).isoformat()
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

    def test_backfill_uses_entry_time_and_ignores_future_candle(self):
        position = missing()
        positions, lines, report = backfill_records(
            [position], [], {}, CUTOFF, lambda _: frame_with_future_pattern(),
        )
        self.assertEqual(report["updated_positions"], 1)
        self.assertEqual(positions[0]["daily_pattern"]["kind"], "bullish_engulfing")
        self.assertLessEqual(
            positions[0]["daily_pattern"]["candle_close_time"],
            parse_time_ms(position["entry_time"]),
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
            "event": "entry_filled", "time": "2026-08-01T08:10:33+00:00",
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

    def test_recent_trade_without_unique_entry_event_aborts(self):
        trade = json.dumps(closed_trade(signal_key="MISSING"))
        with self.assertRaises(RuntimeError):
            backfill_records(
                [], [trade], {}, CUTOFF, lambda _: frame_with_future_pattern(),
            )
