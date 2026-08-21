import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from momentum_compression_store import default_state, save_compression_state

with patch("trader.SqueezeBreakoutBot.start"):
    import web_ui


def _fresh_event(alignment="CONFIRMED"):
    return {
        "compression_id": "BTCUSDT:LONG:1",
        "state": "BREAKOUT_FRESH_LONG",
        "event_at": 10,
        "live_price": 101.0,
        "htf_alignment": alignment,
        "structure": {"symbol": "BTCUSDT", "side": "LONG"},
    }


class CompressionApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web_ui.app.test_client()

    def _login(self, role="user"):
        with self.client.session_transaction() as state:
            state["user_id"] = 9
            state["role"] = role

    def test_alerts_require_login(self):
        self.assertEqual(self.client.get("/api/compression/alerts?after=0").status_code, 401)

    def test_automation_is_admin_only_and_requires_json_boolean(self):
        self._login()
        self.assertEqual(self.client.post("/api/compression/automation", json={"enabled": True}).status_code, 403)
        self._login("admin")
        self.assertEqual(self.client.post("/api/compression/automation", json={"enabled": 1}).status_code, 400)
        with patch.object(web_ui._compression_monitor, "set_auto_enabled", return_value={"auto_enabled": True}) as set_enabled:
            response = self.client.post("/api/compression/automation", json={"enabled": True})
        self.assertEqual(response.status_code, 200)
        set_enabled.assert_called_once_with(True)

    def test_status_is_logged_in_and_discloses_management_capability_only(self):
        self.assertEqual(self.client.get("/api/compression/status").status_code, 401)
        self._login("admin")
        with patch.object(web_ui._compression_monitor, "status", return_value={"running": False}):
            response = self.client.get("/api/compression/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["can_manage"])

    def test_only_15m_manual_compression_scan_is_valid(self):
        self.assertEqual(self.client.get("/scan/compression/1h").status_code, 400)
        with patch.object(web_ui._compression_monitor, "scan_now", return_value=True) as scan_now:
            response = self.client.get("/scan/compression/15m")
        self.assertEqual(response.status_code, 200)
        scan_now.assert_called_once_with("manual")

    def test_overlapping_manual_scans_start_only_one_monitor_scan(self):
        started, release = threading.Event(), threading.Event()
        def scan_now(trigger):
            started.set(); release.wait(1); return True
        with patch.object(web_ui._compression_monitor, "scan_now", side_effect=scan_now) as scan:
            first = threading.Thread(target=lambda: self.client.get("/scan/compression/15m"))
            first.start(); self.assertTrue(started.wait(1))
            second = self.client.get("/scan/compression/15m")
            release.set(); first.join(2)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(scan.call_count, 1)


class CompressionLifecycleTests(unittest.TestCase):
    def setUp(self):
        web_ui._stop_compression_monitor_for_tests()
        web_ui._stop_compression_alert_worker_for_tests()
        web_ui._compression_alert_wakeup.clear()

    def tearDown(self):
        web_ui._stop_compression_monitor_for_tests()
        web_ui._stop_compression_alert_worker_for_tests()

    def test_confirmed_events_append_alerts_and_wake_independent_worker(self):
        with patch.object(web_ui, "append_compression_alerts", return_value=[dict(_fresh_event())]) as append:
            web_ui._compression_alert_wakeup.clear()
            created = web_ui._process_compression_events([_fresh_event()], 11)
        self.assertEqual(len(created), 1)
        append.assert_called_once()
        self.assertTrue(web_ui._compression_alert_wakeup.is_set())

    def test_unconfirmed_events_do_not_wake_delivery_worker(self):
        with patch.object(web_ui, "append_compression_alerts", return_value=[_fresh_event("UNKNOWN")]):
            web_ui._compression_alert_wakeup.clear()
            web_ui._process_compression_events([_fresh_event("UNKNOWN")], 11)
        self.assertFalse(web_ui._compression_alert_wakeup.is_set())

    def test_start_helpers_create_only_one_monitor_and_delivery_worker(self):
        with patch.object(web_ui._compression_monitor, "start", return_value=True) as start:
            self.assertTrue(web_ui._start_compression_monitor())
            self.assertFalse(web_ui._start_compression_monitor())
        self.assertEqual(start.call_count, 1)
        threads = []
        class DeferredThread:
            def __init__(self, target=None, daemon=None): self.alive = False; threads.append(self)
            def start(self): self.alive = True
            def is_alive(self): return self.alive
            def join(self, timeout=None): self.alive = False
        with patch.object(web_ui.threading, "Thread", DeferredThread):
            self.assertTrue(web_ui._start_compression_alert_worker())
            self.assertFalse(web_ui._start_compression_alert_worker())
        self.assertEqual(len(threads), 1)

    def test_stop_helpers_finish_within_two_seconds(self):
        with patch.object(web_ui._compression_monitor, "stop", return_value=True):
            started = time.monotonic()
            web_ui._stop_compression_monitor_for_tests()
        self.assertLess(time.monotonic() - started, 2)

        class StoppedThread:
            def is_alive(self): return False

        web_ui._compression_alert_thread = StoppedThread()
        started = time.monotonic()
        web_ui._stop_compression_alert_worker_for_tests()
        self.assertLess(time.monotonic() - started, 2)

    def test_startup_loads_auto_enabled_without_replaying_historical_events(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "compression.json"
            state = default_state(); state["auto_enabled"] = True; state["next_event_id"] = 5
            save_compression_state(state_path, state)
            with patch.object(web_ui, "MOMENTUM_COMPRESSION_STATE", state_path), \
                 patch.object(web_ui._compression_monitor, "start", return_value=True) as start:
                self.assertTrue(web_ui._start_compression_monitor())
            start.assert_called_once()
            self.assertFalse(web_ui._compression_alert_wakeup.is_set())

    def test_import_alone_starts_no_compression_threads(self):
        environment = os.environ.copy(); environment["PYTHONPATH"] = str(Path.cwd())
        result = subprocess.run([sys.executable, "-c", "from unittest.mock import patch\nwith patch('trader.SqueezeBreakoutBot.start'):\n import web_ui\nassert web_ui._compression_alert_thread is None"], cwd=Path.cwd(), env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
