import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from momentum_compression_store import default_state, save_compression_state


def pool_item(symbol="POOLUSDT"):
    return {
        "symbol": symbol, "side": "LONG", "compression_id": "pool-long",
        "state": "PRE_BREAKOUT", "fresh_emitted": False,
        "upper_boundary_price": 11.0, "lower_boundary_price": 9.0,
        "breakout_buffer_price": 0.5, "first_seen_at": 1,
        "last_verified_at": 1,
    }


class SchedulerTests(unittest.TestCase):
    def test_next_closed_15m_scan_waits_five_seconds_after_close_in_utc(self):
        from momentum_compression_monitor import next_closed_15m_scan_at

        self.assertEqual(
            next_closed_15m_scan_at(datetime(2026, 8, 21, 12, 14, 59, tzinfo=timezone.utc)),
            datetime(2026, 8, 21, 12, 15, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_closed_15m_scan_at(datetime(2026, 8, 21, 12, 15, 5, tzinfo=timezone.utc)),
            datetime(2026, 8, 21, 12, 30, 5, tzinfo=timezone.utc),
        )

    def test_next_closed_15m_scan_normalizes_non_utc_aware_time(self):
        from momentum_compression_monitor import next_closed_15m_scan_at

        beijing = timezone(timedelta(hours=8))
        self.assertEqual(
            next_closed_15m_scan_at(datetime(2026, 8, 21, 20, 14, 59, tzinfo=beijing)),
            datetime(2026, 8, 21, 12, 15, 5, tzinfo=timezone.utc),
        )


class CompressionMonitorPriceTests(unittest.TestCase):
    def _monitor(self, root, *, with_pool=True, **kwargs):
        from momentum_compression_monitor import CompressionMonitor

        state_path = root / "state.json"
        state = default_state()
        if with_pool:
            state["pool"]["pool-long"] = pool_item()
        save_compression_state(state_path, state)
        return CompressionMonitor(state_path, root, lambda events: None, **kwargs)

    def test_message_filters_to_pool_before_applying_prices(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            applied = {}
            monitor._apply_prices = lambda prices, now_ms: applied.update(prices)
            monitor.handle_message(json.dumps([
                {"s": "POOLUSDT", "c": "10.5"},
                {"s": "OTHERUSDT", "c": "99"},
            ]), now_ms=2_000)

        self.assertEqual(applied, {"POOLUSDT": 10.5})

    def test_open_and_valid_message_update_stream_heartbeat(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), time_ms=lambda: 1_000)
            app = object()
            monitor._app = app
            monitor._on_open(app)
            self.assertEqual(monitor.status()["last_price_message_at"], 1_000)

            monitor.handle_message(
                json.dumps([{"s": "OTHERUSDT", "c": "99"}]),
                now_ms=2_000,
            )

        self.assertEqual(monitor.status()["last_price_message_at"], 2_000)

    def test_malformed_or_nonfinite_message_does_not_refresh_stream_heartbeat(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), time_ms=lambda: 1_000)
            app = object()
            monitor._app = app
            monitor._on_open(app)
            monitor.handle_message(json.dumps([{"s": "POOLUSDT", "c": "bad"}]), now_ms=2_000)

        self.assertEqual(monitor.status()["last_price_message_at"], 1_000)

    def test_rest_fallback_filters_pool_prices(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"symbol": "POOLUSDT", "price": "10.5"}, {"symbol": "OTHERUSDT", "price": "99"}]

        calls = []
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), http_get=lambda *args, **kwargs: calls.append(args) or Response())
            applied = {}
            monitor._apply_prices = lambda prices, now_ms: applied.update(prices)
            monitor.rest_fallback_once(now_ms=2_000)

        self.assertEqual(len(calls), 1)
        self.assertEqual(applied, {"POOLUSDT": 10.5})

    def test_rest_fallback_skips_request_when_pool_is_empty(self):
        calls = []
        with TemporaryDirectory() as folder:
            monitor = self._monitor(
                Path(folder),
                with_pool=False,
                http_get=lambda *args, **kwargs: calls.append(args),
            )
            result = monitor.rest_fallback_once(now_ms=2_000)

        self.assertTrue(result)
        self.assertEqual(calls, [])

    def test_fallback_cycle_uses_rest_even_while_stream_is_connected(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), time_ms=lambda: 2_000)
            monitor._stream_connected = True
            calls = []
            monitor.rest_fallback_once = lambda **kwargs: calls.append(kwargs) or True
            monitor.sleep = lambda seconds: monitor._stop.set()
            monitor._fallback_loop()

        self.assertEqual(calls, [{"now_ms": 2_000}])

    def test_connected_stream_is_closed_once_after_fifteen_seconds_without_prices(self):
        class App:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), time_ms=lambda: 16_000)
            app = App()
            monitor._app = app
            monitor._stream_connected = True
            monitor._price_stream_status = "connected"
            monitor._last_stream_message_at_ms = 1_000

            self.assertTrue(monitor._close_stale_stream(16_000))
            self.assertFalse(monitor._close_stale_stream(16_001))

        self.assertEqual(app.close_calls, 1)
        self.assertFalse(monitor._stream_connected)
        self.assertEqual(monitor.status()["price_stream_status"], "stale")

    def test_fresh_message_linearized_before_stale_check_keeps_current_stream_open(self):
        class App:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            app = App()
            monitor._app = app
            monitor._stream_connected = True
            monitor._price_stream_status = "connected"
            monitor._last_stream_message_at_ms = 1_000

            monitor.handle_message(
                json.dumps([{"s": "POOLUSDT", "c": "10.5"}]),
                now_ms=16_000,
                source_app=app,
            )
            self.assertFalse(monitor._close_stale_stream(16_000))

        self.assertEqual(app.close_calls, 0)
        self.assertTrue(monitor._stream_connected)
        self.assertEqual(monitor.status()["price_stream_status"], "connected")
        self.assertEqual(monitor.status()["last_price_message_at"], 16_000)

    def test_stale_close_intent_prevents_late_current_message_from_restoring_health(self):
        class App:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            app = App()
            monitor._app = app
            monitor._stream_connected = True
            monitor._price_stream_status = "connected"
            monitor._last_stream_message_at_ms = 1_000

            self.assertTrue(monitor._close_stale_stream(16_000))
            monitor.handle_message(
                json.dumps([{"s": "POOLUSDT", "c": "10.5"}]),
                now_ms=16_001,
                source_app=app,
            )

        self.assertEqual(app.close_calls, 1)
        self.assertFalse(monitor._stream_connected)
        self.assertEqual(monitor.status()["price_stream_status"], "stale")
        self.assertEqual(monitor.status()["last_price_message_at"], 1_000)

    def test_delayed_callbacks_from_old_app_cannot_mutate_current_app_state(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), time_ms=lambda: 3_000)
            old_app = object()
            current_app = object()
            monitor._app = current_app
            monitor._stream_connected = True
            monitor._stream_ever_connected = True
            monitor._price_stream_status = "connected"
            monitor._last_stream_message_at_ms = 2_000

            monitor._on_open(old_app)
            monitor._on_message(old_app, json.dumps([{"s": "POOLUSDT", "c": "10.5"}]))
            monitor._on_error(old_app, RuntimeError("old app failure"))
            monitor._on_close(old_app)

        self.assertTrue(monitor._stream_connected)
        self.assertTrue(monitor._stream_ever_connected)
        self.assertEqual(monitor.status()["price_stream_status"], "connected")
        self.assertEqual(monitor.status()["last_price_message_at"], 2_000)
        self.assertEqual(monitor.status()["last_error"], "")

    def test_websocket_factory_failure_leaves_no_stale_app_ownership(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))

            def websocket_factory(url, **callbacks):
                raise RuntimeError("factory unavailable")

            monitor.websocket_factory = websocket_factory
            monitor.sleep = lambda delay: monitor._stop.set()
            monitor._stream_loop()

        self.assertIsNone(monitor._app)
        self.assertEqual(monitor.status()["last_error"], "WebSocket: factory unavailable")

    def test_finished_socket_releases_current_app_before_stopping(self):
        class App:
            def run_forever(self):
                monitor._stop.set()

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            monitor.websocket_factory = lambda url, **callbacks: App()
            monitor._stream_loop()

        self.assertIsNone(monitor._app)

    def test_stale_close_callback_stays_stale_before_the_next_socket_reconnects(self):
        apps, lifecycle_states, sleep_states = [], [], []
        app_started = threading.Event()
        allow_open = threading.Event()
        opened = threading.Event()
        proceed_to_stale_close = threading.Event()

        class App:
            def __init__(self, **callbacks):
                self.callbacks = callbacks
                apps.append(self)

            def close(self):
                self.callbacks["on_close"](self)

            def run_forever(self):
                if len(apps) == 1:
                    app_started.set()
                    allow_open.wait(1)
                    self.callbacks["on_open"](self)
                    opened.set()
                    proceed_to_stale_close.wait(1)
                    self.callbacks["on_message"](
                        self,
                        json.dumps([{"s": "POOLUSDT", "c": "10.5"}]),
                    )
                    if not monitor._close_stale_stream(16_000):
                        raise AssertionError("expected stale stream close")
                    lifecycle_states.append(monitor._price_stream_status)
                    return
                lifecycle_states.append(monitor._price_stream_status)
                self.callbacks["on_open"](self)
                self.callbacks["on_close"](self)
                lifecycle_states.append(monitor._price_stream_status)
                monitor._stop.set()

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), time_ms=lambda: 1_000)
            def websocket_factory(url, **callbacks):
                return App(**callbacks)

            monitor.websocket_factory = websocket_factory
            monitor.sleep = lambda delay: sleep_states.append(monitor._price_stream_status)

            worker = threading.Thread(target=monitor._stream_loop)
            worker.start()
            self.assertTrue(app_started.wait(1))
            monitor._lifecycle_lock.acquire()
            try:
                allow_open.set()
                self.assertFalse(opened.wait(0.05))
            finally:
                monitor._lifecycle_lock.release()

            self.assertTrue(opened.wait(1))
            proceed_to_stale_close.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(lifecycle_states, ["stale", "connecting", "reconnecting"])
        self.assertEqual(sleep_states, ["stale"])

    def test_tree_rest_breakout_emits_once_when_websocket_repeats_the_price(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"symbol": "TREEUSDT", "price": "0.03972"}]

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = default_state()
            state["pool"]["tree-long"] = {
                **pool_item("TREEUSDT"),
                "compression_id": "tree-long",
                "upper_boundary_price": 0.03969363636363638,
                "lower_boundary_price": 0.03945749999999999,
                "breakout_buffer_price": 0.000010545513981230876,
                "htf_alignment": "UNKNOWN",
            }
            save_compression_state(root / "state.json", state)
            callbacks = []
            from momentum_compression_monitor import CompressionMonitor
            monitor = CompressionMonitor(
                root / "state.json",
                root,
                callbacks.extend,
                http_get=lambda *args, **kwargs: Response(),
                time_ms=lambda: 2_000,
            )
            monitor._stream_connected = True
            monitor._last_stream_message_at_ms = 2_000
            monitor.sleep = lambda seconds: monitor._stop.set()
            monitor._fallback_loop()
            monitor.handle_message(
                json.dumps([{"s": "TREEUSDT", "c": "0.03973"}]),
                now_ms=2_100,
            )

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0]["state"], "BREAKOUT_FRESH_LONG")
        self.assertEqual(callbacks[0]["live_price"], 0.03972)

    def test_reconnect_delays_follow_documented_caps(self):
        from momentum_compression_monitor import CompressionMonitor

        self.assertEqual([CompressionMonitor.reconnect_delay(attempt) for attempt in range(7)], [1, 2, 5, 10, 30, 30, 30])

    def test_malformed_or_non_pool_rows_are_counted_without_stopping_updates(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            monitor.handle_message(json.dumps([
                {"s": "POOLUSDT", "c": "bad"},
                {"s": "OTHERUSDT", "c": "99"},
                "bad-row",
            ]), now_ms=2_000)
            status = monitor.status()

        self.assertEqual(status["dropped_price_rows"], 3)

    def test_event_callback_can_read_status_after_state_is_saved(self):
        from momentum_compression_monitor import CompressionMonitor

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / "state.json"
            state = default_state()
            state["pool"]["pool-long"] = {**pool_item(), "htf_alignment": "CONFIRMED"}
            save_compression_state(state_path, state)
            callbacks = []
            monitor = CompressionMonitor(state_path, root, lambda events: callbacks.append((events, monitor.status())))
            monitor.handle_message(json.dumps([{"s": "POOLUSDT", "c": "12"}]), now_ms=2_000)

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0][0]["state"], "BREAKOUT_FRESH_LONG")
        self.assertEqual(callbacks[0][1]["today_fresh"], 0)

    def test_disabling_auto_stops_monitoring_but_preserves_pool(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = self._monitor(root)
            status = monitor.set_auto_enabled(False)
            self.assertFalse(status["running"])
            self.assertEqual(status["pool_size"], 1)

    def test_overflow_price_row_is_dropped_and_later_valid_price_is_applied(self):
        class OverflowingValue:
            def __float__(self):
                raise OverflowError("too large")

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            applied = {}
            monitor._apply_prices = lambda prices, now_ms: applied.update(prices)
            self.assertIsNone(monitor._finite_price(OverflowingValue()))
            monitor.handle_message(json.dumps([
                {"s": "POOLUSDT", "c": "1e1000000"},
                {"s": "POOLUSDT", "c": "10.5"},
            ]), now_ms=2_000)

        self.assertEqual(applied, {"POOLUSDT": 10.5})


class CompressionMonitorScanTests(unittest.TestCase):
    def _monitor(self, root, scan, callback=lambda events: None):
        from momentum_compression_monitor import CompressionMonitor

        state_path = root / "state.json"
        save_compression_state(state_path, default_state())
        return CompressionMonitor(state_path, root, callback, scan=scan)

    def test_scan_network_work_does_not_block_live_price_updates(self):
        with TemporaryDirectory() as folder:
            scan_started = threading.Event()
            release_scan = threading.Event()
            price_finished = threading.Event()

            def scan(state_path, snapshot_dir):
                scan_started.set()
                release_scan.wait(1)
                return {"evaluated_at": 1, "events": []}

            monitor = self._monitor(Path(folder), scan)
            scan_thread = threading.Thread(target=lambda: monitor.scan_now("manual"))
            price_thread = threading.Thread(target=lambda: (monitor._apply_prices({"POOLUSDT": 10.5}, 2), price_finished.set()))
            scan_thread.start()
            self.assertTrue(scan_started.wait(1))
            price_thread.start()
            self.assertTrue(price_finished.wait(0.2))
            release_scan.set()
            scan_thread.join(1)
            price_thread.join(1)

        self.assertTrue(price_finished.is_set())

    def test_status_tracks_scan_timing_and_marks_held_lock_overdue(self):
        clock = [1_000]
        report = {"evaluated_at": 1, "events": []}
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), lambda *args: report)
            monitor.time_ms = lambda: clock[0]
            self.assertTrue(monitor.scan_now("manual"))
            status = monitor.status()
            self.assertEqual(status["scan_started_at"], 1_000)
            self.assertEqual(status["scan_duration_ms"], 0)
            self.assertFalse(status["scan_overdue"])

            monitor._scan_lock.acquire()
            try:
                clock[0] += 900_000
                overdue = monitor.status()
            finally:
                monitor._scan_lock.release()

        self.assertTrue(overdue["scan_overdue"])

    def test_scan_dispatches_only_events_newer_than_enable_cursor_baseline(self):
        events = []
        reports = iter((
            {"evaluated_at": 1, "events": [{"event_id": 1, "name": "old"}]},
            {"evaluated_at": 2, "events": [{"event_id": 2, "name": "new"}]},
        ))
        with TemporaryDirectory() as folder:
            root = Path(folder)
            class DormantThread:
                def __init__(self, **kwargs):
                    return None

                def start(self):
                    return None

                def join(self, timeout):
                    return None

                def is_alive(self):
                    return False

            monitor = self._monitor(root, lambda *args: next(reports), events.extend)
            monitor.thread_factory = DormantThread
            state = default_state()
            state["next_event_id"] = 2
            save_compression_state(root / "state.json", state)
            monitor.set_auto_enabled(True)
            monitor.stop()
            monitor.scan_now("manual")
            monitor.scan_now("manual")

        self.assertEqual(events, [{"event_id": 2, "name": "new"}])

    def test_start_baselines_persisted_auto_enabled_events_before_threads_begin(self):
        events = []
        reports = iter((
            {"evaluated_at": 1, "events": [{"event_id": 1, "name": "historic"}]},
            {"evaluated_at": 2, "events": [{"event_id": 2, "name": "new"}]},
        ))

        class DormantThread:
            def __init__(self, **kwargs):
                return None

            def start(self):
                return None

            def join(self, timeout):
                return None

            def is_alive(self):
                return False

        with TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = self._monitor(root, lambda *args: next(reports), events.extend)
            monitor.thread_factory = DormantThread
            state = default_state()
            state["auto_enabled"] = True
            state["next_event_id"] = 2
            save_compression_state(root / "state.json", state)
            self.assertTrue(monitor.start())
            monitor.stop()
            monitor.scan_now("manual")
            monitor.scan_now("manual")

        self.assertEqual(events, [{"event_id": 2, "name": "new"}])

    def test_reenable_after_timed_out_stop_starts_one_new_generation_when_old_workers_exit(self):
        created = []

        class ControlledThread:
            def __init__(self, *, target, **kwargs):
                self.target = target
                self.alive = False
                created.append(self)

            def start(self):
                self.alive = True

            def join(self, timeout):
                return None

            def is_alive(self):
                return self.alive

        with TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = self._monitor(root, lambda *args: {"events": []})
            monitor.thread_factory = ControlledThread
            monitor._stream_loop = lambda: None
            monitor._fallback_loop = lambda: None
            monitor._scheduler_loop = lambda: None
            state = default_state()
            state["auto_enabled"] = True
            save_compression_state(root / "state.json", state)
            self.assertTrue(monitor.start())
            self.assertFalse(monitor.stop(timeout=0))
            monitor.set_auto_enabled(True)
            self.assertEqual(len(created), 3)
            for worker in list(created):
                worker.target()

        self.assertEqual(len(created), 6)
        self.assertTrue(monitor.status()["running"])

    def test_stop_uses_one_total_timeout_budget_for_blocking_workers(self):
        class BlockingThread:
            def __init__(self):
                self.join_timeouts = []

            def join(self, timeout):
                self.join_timeouts.append(timeout)
                clock[0] += timeout

            def is_alive(self):
                return True

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), lambda *args: {"events": []})
            workers = [BlockingThread() for _ in range(3)]
            monitor._threads = workers
            monitor._running = True
            monitor._active_worker_count = 3
            clock = [100.0]
            with patch("momentum_compression_monitor.time.monotonic", side_effect=lambda: clock[0]):
                self.assertFalse(monitor.stop(timeout=2))

        self.assertEqual(workers[0].join_timeouts, [2])
        self.assertEqual(workers[1].join_timeouts, [])
        self.assertEqual(workers[2].join_timeouts, [])

    def test_disable_during_worker_start_never_joins_an_unstarted_thread(self):
        created = []
        first_started = threading.Event()
        allow_first_start = threading.Event()
        disable_attempted = threading.Event()
        join_called = threading.Event()
        disable_errors = []

        class BlockingThread:
            def __init__(self, *, target, **kwargs):
                self.target = target
                self.started = False
                self.finished = False
                created.append(self)

            def start(self):
                self.started = True
                if len(created) == 3 and self is created[0]:
                    first_started.set()
                    allow_first_start.wait(1)

            def join(self, timeout):
                join_called.set()
                if not self.started:
                    raise RuntimeError("cannot join thread before it is started")
                if not self.finished:
                    self.finished = True
                    self.target()

            def is_alive(self):
                return self.started and not self.finished

        with TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = self._monitor(root, lambda *args: {"events": []})
            monitor.thread_factory = BlockingThread
            monitor._stream_loop = lambda: None
            monitor._fallback_loop = lambda: None
            monitor._scheduler_loop = lambda: None
            state = default_state()
            state["auto_enabled"] = True
            save_compression_state(root / "state.json", state)
            starter = threading.Thread(target=monitor.start)
            starter.start()
            self.assertTrue(first_started.wait(1))

            def disable():
                disable_attempted.set()
                try:
                    monitor.set_auto_enabled(False)
                except Exception as error:
                    disable_errors.append(error)

            disabler = threading.Thread(target=disable)
            disabler.start()
            self.assertTrue(disable_attempted.wait(1))
            try:
                self.assertFalse(join_called.wait(0.05))
            finally:
                allow_first_start.set()
                starter.join(1)
                disabler.join(1)

        self.assertEqual(disable_errors, [])
        self.assertTrue(all(worker.started and worker.finished for worker in created))
        self.assertFalse(monitor.status()["running"])

    def test_successful_socket_session_resets_next_retry_delay_to_one_second(self):
        from momentum_compression_monitor import CompressionMonitor

        delays, apps = [], []

        class App:
            def __init__(self, **callbacks):
                self.callbacks = callbacks
                apps.append(self)

            def run_forever(self):
                if len(apps) == 1:
                    raise RuntimeError("offline")
                self.callbacks["on_open"](self)
                self.callbacks["on_close"](self)

        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), lambda *args: {"events": []})
            monitor.websocket_factory = lambda url, **callbacks: App(**callbacks)

            def sleep(delay):
                delays.append(delay)
                if len(delays) == 2:
                    monitor._stop.set()

            monitor.sleep = sleep
            monitor._stream_loop()

        self.assertEqual(delays, [1, 1])


if __name__ == "__main__":
    unittest.main()
