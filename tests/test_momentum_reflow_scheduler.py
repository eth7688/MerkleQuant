import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

with patch("trader.SqueezeBreakoutBot.start"):
    import web_ui


BEIJING = ZoneInfo("Asia/Shanghai")


def milliseconds(value):
    return int(
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    )


class ReflowSchedulerStepTests(unittest.TestCase):
    def test_high_worker_failure_rate_sets_automation_error(self):
        with web_ui._reflow_automation_lock:
            web_ui._reflow_automation["last_auto_error"] = ""
            web_ui._reflow_automation["last_auto_scan_at"] = 0

        with patch.object(web_ui.time, "time", return_value=123.0):
            web_ui._update_reflow_automation_success(
                "auto", {"scanned": 71, "errors": 69}
            )

        with web_ui._reflow_automation_lock:
            self.assertEqual(web_ui._reflow_automation["last_auto_scan_at"], 123000)
            self.assertIn("69/71", web_ui._reflow_automation["last_auto_error"])

    def test_small_worker_failure_rate_does_not_mark_whole_scan_failed(self):
        with web_ui._reflow_automation_lock:
            web_ui._reflow_automation["last_auto_error"] = "stale"

        web_ui._update_reflow_automation_success(
            "auto", {"scanned": 71, "errors": 1}
        )

        with web_ui._reflow_automation_lock:
            self.assertEqual(web_ui._reflow_automation["last_auto_error"], "")

    def test_enabled_scheduler_runs_immediately_then_at_next_hh03(self):
        starts = []
        state = web_ui._new_reflow_scheduler_state()

        state = web_ui._reflow_scheduler_step(
            datetime(2026, 7, 30, 10, 1, tzinfo=BEIJING),
            {"auto_scan_enabled": True},
            state,
            lambda: starts.append("scan") or True,
        )

        self.assertEqual(starts, ["scan"])
        self.assertEqual(
            state["next_scan_at"],
            milliseconds("2026-07-30T02:03:00Z"),
        )

    def test_disabled_scheduler_makes_no_scan_request(self):
        state = web_ui._new_reflow_scheduler_state()
        starts = []

        for minute in (1, 2, 3):
            state = web_ui._reflow_scheduler_step(
                datetime(2026, 7, 30, 10, minute, tzinfo=BEIJING),
                {"auto_scan_enabled": False},
                state,
                lambda: starts.append("scan") or True,
            )

        self.assertEqual(starts, [])

    def test_reenable_triggers_one_immediate_scan(self):
        state = web_ui._new_reflow_scheduler_state(previous_enabled=False)
        starts = []

        state = web_ui._reflow_scheduler_step(
            datetime(2026, 7, 30, 10, 10, tzinfo=BEIJING),
            {"auto_scan_enabled": True},
            state,
            lambda: starts.append("scan") or True,
        )

        self.assertEqual(starts, ["scan"])

    def test_busy_slot_is_recorded_as_skipped_without_queue(self):
        state = web_ui._new_reflow_scheduler_state(
            previous_enabled=True,
            next_scan_at=milliseconds("2026-07-30T02:03:00Z"),
        )

        state = web_ui._reflow_scheduler_step(
            datetime(2026, 7, 30, 10, 3, tzinfo=BEIJING),
            {"auto_scan_enabled": True},
            state,
            lambda: False,
        )

        self.assertEqual(
            state["last_skip_at"],
            milliseconds("2026-07-30T02:03:00Z"),
        )
        self.assertEqual(
            state["next_scan_at"],
            milliseconds("2026-07-30T03:03:00Z"),
        )

    def test_due_slot_is_attempted_only_once(self):
        starts = []
        now = datetime(2026, 7, 30, 10, 3, tzinfo=BEIJING)
        slot = milliseconds("2026-07-30T02:03:00Z")
        state = web_ui._new_reflow_scheduler_state(
            previous_enabled=True,
            next_scan_at=slot,
        )

        state = web_ui._reflow_scheduler_step(
            now,
            {"auto_scan_enabled": True},
            state,
            lambda: starts.append("scan") or False,
        )
        state = web_ui._reflow_scheduler_step(
            now,
            {"auto_scan_enabled": True},
            state,
            lambda: starts.append("scan") or True,
        )

        self.assertEqual(starts, ["scan"])
        self.assertEqual(state["last_attempt_slot"], slot)

    def test_previously_attempted_due_slot_advances_without_retry(self):
        slot = milliseconds("2026-07-30T02:03:00Z")
        state = web_ui._new_reflow_scheduler_state(
            previous_enabled=True,
            next_scan_at=slot,
            last_attempt_slot=slot,
        )
        starts = []

        state = web_ui._reflow_scheduler_step(
            datetime(2026, 7, 30, 10, 3, tzinfo=BEIJING),
            {"auto_scan_enabled": True},
            state,
            lambda: starts.append("scan") or True,
        )

        self.assertEqual(starts, [])
        self.assertEqual(
            state["next_scan_at"],
            milliseconds("2026-07-30T03:03:00Z"),
        )


class ReflowSchedulerLifecycleTests(unittest.TestCase):
    def setUp(self):
        web_ui._stop_reflow_scheduler_for_tests()

    def tearDown(self):
        web_ui._stop_reflow_scheduler_for_tests()

    def test_start_called_twice_creates_one_thread(self):
        threads = []

        class DeferredThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.alive = False
                threads.append(self)

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                self.alive = False

        with patch.object(web_ui.threading, "Thread", DeferredThread):
            self.assertTrue(web_ui._start_reflow_scheduler())
            self.assertFalse(web_ui._start_reflow_scheduler())

        self.assertEqual(len(threads), 1)

    def test_stop_does_not_forget_a_thread_that_failed_to_exit(self):
        class StubbornThread:
            alive = True

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                pass

        worker = StubbornThread()
        web_ui._reflow_scheduler_thread = worker
        with web_ui._reflow_automation_lock:
            web_ui._reflow_automation["running"] = True

        web_ui._stop_reflow_scheduler_for_tests()

        self.assertIs(web_ui._reflow_scheduler_thread, worker)
        self.assertTrue(web_ui._reflow_automation["running"])
        worker.alive = False

    def test_import_alone_does_not_start_scheduler_thread(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path.cwd())

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from unittest.mock import patch\n"
                    "with patch('trader.SqueezeBreakoutBot.start'):\n"
                    "    import web_ui\n"
                    "assert web_ui._reflow_scheduler_thread is None"
                ),
            ],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_invalid_settings_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            with patch.object(web_ui, "MOMENTUM_REFLOW_SETTINGS", path):
                response = web_ui.app.test_client().get(
                    "/api/reflow/automation/status"
                )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["auto_scan_enabled"])
        self.assertIn("error", response.get_json())


class ReflowAlertWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        web_ui._reflow_alert_stop.clear()
        web_ui._reflow_alert_wakeup.clear()
        with web_ui._reflow_alert_lock:
            web_ui._reflow_alert_status["last_error"] = ""

    def tearDown(self):
        web_ui._stop_reflow_alert_worker_for_tests()

    def test_import_alone_does_not_start_alert_worker(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path.cwd())

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from unittest.mock import patch\n"
                    "with patch('trader.SqueezeBreakoutBot.start'):\n"
                    "    import web_ui\n"
                    "assert web_ui._reflow_alert_thread is None"
                ),
            ],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_start_called_twice_creates_one_alert_thread(self):
        threads = []

        class DeferredThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.alive = False
                threads.append(self)

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                self.alive = False

        with patch.object(web_ui.threading, "Thread", DeferredThread):
            self.assertTrue(web_ui._start_reflow_alert_worker())
            self.assertFalse(web_ui._start_reflow_alert_worker())

        self.assertEqual(len(threads), 1)

    def test_worker_masks_webhook_and_caps_returned_delivery_error(self):
        unsafe_error = (
            "delivery failure https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
            "key=leaked-returned-key " + "x" * 100
        )

        def stop_after_wait(timeout):
            web_ui._reflow_alert_stop.set()
            return False

        with patch.object(
            web_ui,
            "deliver_due_wechat",
            return_value={"status": "idle", "error": unsafe_error},
        ), patch.object(web_ui._reflow_alert_wakeup, "wait", stop_after_wait):
            web_ui._reflow_alert_worker_loop()

        status_error = web_ui._reflow_alert_status["last_error"]
        self.assertNotIn("leaked", status_error)
        self.assertLessEqual(len(status_error), 80)

    def test_worker_masks_webhook_and_caps_raised_delivery_error(self):
        unsafe_error = (
            "delivery failure https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
            "key=leaked-raised-key " + "x" * 100
        )

        def stop_after_wait(timeout):
            web_ui._reflow_alert_stop.set()
            return False

        with patch.object(
            web_ui,
            "deliver_due_wechat",
            side_effect=RuntimeError(unsafe_error),
        ), patch.object(web_ui._reflow_alert_wakeup, "wait", stop_after_wait):
            web_ui._reflow_alert_worker_loop()

        status_error = web_ui._reflow_alert_status["last_error"]
        self.assertNotIn("leaked", status_error)
        self.assertLessEqual(len(status_error), 80)


if __name__ == "__main__":
    unittest.main()
