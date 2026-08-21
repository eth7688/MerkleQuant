import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from momentum_compression_store import (
    STATE_VERSION,
    apply_live_prices,
    default_state,
    load_compression_state,
    reconcile_structure_scan,
    save_compression_state,
    write_compression_snapshot,
)


def evaluation(**overrides):
    row = {
        "symbol": "TESTUSDT",
        "side": "LONG",
        "compression_id": "long-episode-1",
        "state": "PRE_BREAKOUT",
        "evaluated_at": 900,
        "htf_alignment": "CONFIRMED",
        "parameter_version": "15m-compression-v1",
        "compression_start_time": 100,
        "compression_end_time": 800,
        "upper_boundary_price": 110.0,
        "lower_boundary_price": 100.0,
        "breakout_buffer_price": 0.5,
        "atr14": 10.0,
        "compression_bars": 20,
        "rejection_reasons": [],
        "directional_touch_times": [100, 400, 700],
    }
    row.update(overrides)
    return row


class CompressionStateMachineTests(unittest.TestCase):
    def test_first_scan_outside_does_not_enter_pool_or_create_event(self):
        state, report = reconcile_structure_scan(
            default_state(), [evaluation(state="OUTSIDE_AT_DISCOVERY")], 1_000,
        )
        self.assertEqual(state["pool"], {})
        self.assertEqual(report["fresh_events"], [])

    def test_pool_crossing_creates_exactly_one_fresh_event(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        state, second = apply_live_prices(state, {"TESTUSDT": 111.0}, 3_000)
        self.assertEqual([event["state"] for event in first], ["BREAKOUT_FRESH_LONG"])
        self.assertEqual(second, [])
        self.assertTrue(state["pool"]["long-episode-1"]["fresh_emitted"])

    def test_unconfirmed_fresh_active_retracing_failed_transition(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        transitions = []
        for price, now_ms in ((110.0, 2_000), (110.6, 3_000), (111.0, 4_000),
                              (110.25, 5_000), (109.5, 6_000)):
            state, events = apply_live_prices(state, {"TESTUSDT": price}, now_ms)
            transitions.append((state["pool"].get("long-episode-1", {}).get("state"), events))
        self.assertEqual([state_name for state_name, _ in transitions], [
            "BREAKOUT_UNCONFIRMED_LONG", "BREAKOUT_FRESH_LONG",
            "BREAKOUT_ACTIVE_LONG", "BREAKOUT_RETRACING_LONG", None,
        ])
        self.assertEqual(transitions[-1][1], [])
        self.assertNotIn("long-episode-1", state["pool"])
        self.assertEqual(state["episodes"]["long-episode-1"]["state"], "BREAKOUT_FAILED")

    def test_adverse_exit_returns_to_pool_when_price_reenters_structure(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, _ = apply_live_prices(state, {"TESTUSDT": 99.9}, 2_000)
        self.assertEqual(state["pool"]["long-episode-1"]["state"], "BOUNDARY_EXIT_ADVERSE_LONG")
        state, events = apply_live_prices(state, {"TESTUSDT": 105.0}, 3_000)
        self.assertEqual(events, [])
        self.assertEqual(state["pool"]["long-episode-1"]["state"], "COMPRESSION_ACTIVE_LONG")

    def test_structure_invalidation_removes_existing_identity(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, report = reconcile_structure_scan(
            state, [evaluation(state="REJECTED", rejection_reasons=["EMA_DIRECTION"])], 2_000,
        )
        self.assertNotIn("long-episode-1", state["pool"])
        self.assertEqual(report["removed_compression_ids"], ["long-episode-1"])

    def test_unconfirmed_structure_evaluation_does_not_remove_existing_pool_identity(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, report = reconcile_structure_scan(
            state, [evaluation(state="BREAKOUT_UNCONFIRMED_LONG")], 2_000,
        )
        self.assertIn("long-episode-1", state["pool"])
        self.assertEqual(state["pool"]["long-episode-1"]["state"], "PRE_BREAKOUT")
        self.assertEqual(report["removed_compression_ids"], [])

    def test_same_identity_boundary_update_does_not_duplicate_or_reset_fresh(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        state, report = reconcile_structure_scan(
            state, [evaluation(upper_boundary_price=111.0, evaluated_at=2_500)], 3_000,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(report["fresh_events"], [])
        self.assertTrue(state["pool"]["long-episode-1"]["fresh_emitted"])
        self.assertEqual(state["pool"]["long-episode-1"]["upper_boundary_price"], 111.0)

    def test_different_identity_can_emit_a_new_event(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        state, _ = reconcile_structure_scan(
            state, [evaluation(compression_id="long-episode-2", compression_start_time=200)], 3_000,
        )
        state, second = apply_live_prices(state, {"TESTUSDT": 110.6}, 4_000)
        self.assertEqual([event["event_id"] for event in first + second], [1, 2])

    def test_replayed_live_prices_after_restart_do_not_emit_again(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            save_compression_state(path, state)
            restarted = load_compression_state(path)
        _, replay = apply_live_prices(restarted, {"TESTUSDT": 110.6}, 3_000)
        self.assertEqual(len(first), 1)
        self.assertEqual(replay, [])

    def test_terminal_identity_rediscovery_cannot_emit_a_second_fresh_event(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        state, _ = apply_live_prices(state, {"TESTUSDT": 105.0}, 3_000)
        self.assertIn("long-episode-1", state["episodes"])

        state, _ = reconcile_structure_scan(state, [evaluation()], 4_000)
        state, replay = apply_live_prices(state, {"TESTUSDT": 110.6}, 5_000)

        self.assertTrue(state["pool"]["long-episode-1"]["fresh_emitted"])
        self.assertEqual([event["event_id"] for event in first], [1])
        self.assertEqual(replay, [])

    def test_structurally_removed_identity_cannot_emit_again_but_new_identity_can(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        state, _ = reconcile_structure_scan(
            state, [evaluation(state="REJECTED")], 3_000,
        )
        self.assertNotIn("long-episode-1", state["pool"])

        state, _ = reconcile_structure_scan(state, [evaluation()], 4_000)
        state, replay = apply_live_prices(state, {"TESTUSDT": 110.6}, 5_000)
        self.assertEqual(replay, [])

        state, _ = reconcile_structure_scan(
            state, [evaluation(compression_id="long-episode-2", compression_start_time=200)], 6_000,
        )
        _, second = apply_live_prices(state, {"TESTUSDT": 110.6}, 7_000)
        self.assertEqual([event["event_id"] for event in first + second], [1, 2])

    def test_nonfinite_live_price_does_not_mutate_pool(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        with self.assertRaises(ValueError):
            apply_live_prices(state, {"TESTUSDT": float("nan")}, 2_000)


class CompressionStatePersistenceTests(unittest.TestCase):
    def test_missing_file_returns_default_state(self):
        with TemporaryDirectory() as folder:
            self.assertEqual(load_compression_state(Path(folder) / "missing.json"), default_state())

    def test_corrupt_or_invalid_state_raises_without_rewrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text('{"version": 99}', encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                load_compression_state(path)
            self.assertEqual(path.read_bytes(), original)

    def test_state_requires_real_boolean_and_nonnegative_integers(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            invalid = default_state()
            invalid["auto_enabled"] = 1
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_compression_state(path)
            invalid = default_state()
            invalid["next_event_id"] = -1
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_compression_state(path)

    def test_nested_pool_records_reject_missing_or_invalid_live_fields(self):
        cases = (
            ("missing symbol", lambda item: item.pop("symbol")),
            ("missing boundary", lambda item: item.pop("upper_boundary_price")),
            ("nonfinite boundary", lambda item: item.__setitem__("upper_boundary_price", float("nan"))),
            ("inverted boundaries", lambda item: item.__setitem__("lower_boundary_price", 110.0)),
            ("negative buffer", lambda item: item.__setitem__("breakout_buffer_price", -0.1)),
        )
        for name, mutate in cases:
            with self.subTest(name=name), TemporaryDirectory() as folder:
                state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
                mutate(state["pool"]["long-episode-1"])
                path = Path(folder) / "state.json"
                path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_compression_state(path)

    def test_nested_episode_records_reject_invalid_identity_and_timestamps(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, _ = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        state, _ = apply_live_prices(state, {"TESTUSDT": 105.0}, 3_000)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for field, value in (("side", "SIDEWAYS"), ("first_seen_at", -1),
                                 ("last_verified_at", float("inf"))):
                with self.subTest(field=field):
                    corrupt = json.loads(json.dumps(state))
                    corrupt["episodes"]["long-episode-1"][field] = value
                    path.write_text(json.dumps(corrupt), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_compression_state(path)

    def test_restart_rejects_contradictory_fresh_registry_before_price_replay(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        state, _ = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
        cases = (
            ("fresh pool missing registry", lambda corrupt: corrupt["emitted_event_ids"].clear()),
            ("registry backed pool not fresh", lambda corrupt: corrupt["pool"]["long-episode-1"].__setitem__("fresh_emitted", False)),
            ("registry event id not yet allocated", lambda corrupt: corrupt["emitted_event_ids"].__setitem__("long-episode-1", corrupt["next_event_id"])),
        )
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for name, mutate in cases:
                with self.subTest(name=name):
                    corrupt = json.loads(json.dumps(state))
                    mutate(corrupt)
                    path.write_text(json.dumps(corrupt), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_compression_state(path)

    def test_restart_rejects_post_fresh_pool_state_without_fresh_marker(self):
        state, _ = reconcile_structure_scan(default_state(), [evaluation()], 1_000)
        item = state["pool"]["long-episode-1"]
        item["state"] = "BREAKOUT_FRESH_LONG"
        item["fresh_emitted"] = False
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_compression_state(path)

    def test_repeated_save_and_load_are_consistent(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = default_state()
            state["auto_enabled"] = True
            state["last_structure_scan_at"] = 12
            save_compression_state(path, state)
            save_compression_state(path, load_compression_state(path))
            self.assertEqual(load_compression_state(path), state)

    def test_failed_replace_does_not_change_existing_state_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_bytes(b'{"old": true}')
            original = path.read_bytes()
            with patch("momentum_compression_store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    save_compression_state(path, default_state())
            self.assertEqual(path.read_bytes(), original)


class CompressionSnapshotTests(unittest.TestCase):
    def test_snapshot_is_replayable_and_byte_stable(self):
        frame = pd.DataFrame({
            "ot": [100, 200], "o": [1.0, 2.0], "h": [2.0, 3.0],
            "l": [0.5, 1.5], "c": [1.5, 2.5], "v": [10.0, 20.0],
        })
        with TemporaryDirectory() as folder:
            root = Path(folder)
            ref = write_compression_snapshot(root, evaluation(), frame)
            path = root / ref
            first = path.read_bytes()
            self.assertEqual(ref, "momentum_compression_snapshots/long-episode-1.json")
            self.assertEqual(write_compression_snapshot(root, evaluation(), frame), ref)
            self.assertEqual(path.read_bytes(), first)
            payload = json.loads(first)
        self.assertEqual(payload["parameter_version"], "15m-compression-v1")
        self.assertEqual(payload["ohlcv"]["c"], [1.5, 2.5])

    def test_snapshot_existing_identity_is_not_overwritten_by_later_data(self):
        frame = pd.DataFrame({
            "ot": [100], "o": [1.0], "h": [2.0], "l": [0.5], "c": [1.5], "v": [10.0],
        })
        changed = frame.assign(c=[99.0])
        with TemporaryDirectory() as folder:
            root = Path(folder)
            ref = write_compression_snapshot(root, evaluation(), frame)
            path = root / ref
            original = path.read_bytes()
            later = evaluation(parameter_version="later-version", symbol="CHANGEDUSDT")
            self.assertEqual(write_compression_snapshot(root, later, changed), ref)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
