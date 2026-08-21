import os
import json
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


def render_compression_payload(payload):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    helpers = source[
        source.index("function escapeRHtml(value)"):
        source.index("function fmtRValue(value,signed)")
    ]
    renderer = source[
        source.index("var COMPRESSION_ALERT_SOUND_KEY="):
        source.index("// ===== SCANNING =====")
    ]
    script = f"""
var nodes={{stats:{{innerHTML:''}},main:{{innerHTML:''}}}};
global.localStorage={{getItem:function(){{return null;}},setItem:function(){{}}}};
global.window={{}};global.document={{getElementById:function(id){{return nodes[id]||null;}}}};
{helpers}
{renderer}
renderMomentumCompression({json.dumps(payload)});
process.stdout.write(JSON.stringify(nodes));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(completed.stdout)


def run_compression_sound_javascript(body):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    script = source[
        source.index("var COMPRESSION_ALERT_SOUND_KEY="):
        source.index("// ===== SCANNING =====")
    ]
    harness = """
const assert=require('assert');let storage={},fetchPayload={latest_alert_id:0,events:[]};
global.localStorage={getItem:k=>storage[k]??null,setItem:(k,v)=>storage[k]=String(v)};
global.window=global;global.document={getElementById:()=>null};
global.fetch=url=>Promise.resolve({ok:true,json:()=>Promise.resolve(fetchPayload)});
global.setInterval=()=>9;global.clearInterval=()=>{};
"""
    completed = subprocess.run(["node", "-e", harness + script + body], check=True, capture_output=True, text=True, encoding="utf-8")
    return completed.stdout


class CompressionDashboardUiTests(unittest.TestCase):
    def test_sidebar_description_and_renderer_are_wired(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn("['compression','动能压缩破位',[['compression_15m','15M 压缩池','P']]", source)
        self.assertIn("function renderMomentumCompression", source)
        self.assertIn("function refreshCompressionStatus", source)
        self.assertIn("function setCompressionAutoEnabled", source)
        self.assertIn("function pollCompressionAlerts", source)
        self.assertIn("axiom_compression_alert_cursor_v1", source)

    def test_renderer_escapes_payload_and_formats_nonfinite_values(self):
        result = render_compression_payload({
            "monitor": {"auto_enabled": True, "running": True, "last_scan_at": 0},
            "can_manage": False,
            "pool_rows": [{"symbol": "<script>alert(1)</script>", "side": "LONG", "state": "PRE_BREAKOUT", "live_price": float("nan"), "compression_bars": float("inf")}],
            "episode_rows": [{"symbol": "SAFEUSDT", "side": "SHORT", "state": "BREAKOUT_FRESH_SHORT", "live_price": 12.5}],
            "rejection_counts": {"<img src=x>": float("inf")},
        })
        rendered = result["stats"]["innerHTML"] + result["main"]["innerHTML"]
        self.assertIn("LONG 观察池", rendered)
        self.assertIn("SHORT 观察池", rendered)
        self.assertIn("新突破", rendered)
        self.assertIn("拒绝统计", rendered)
        self.assertIn("仅管理员可修改", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotRegex(rendered, r"NaN|Infinity")

    def test_sound_enable_baselines_then_plays_once_for_new_batch(self):
        body = """
let plays=0;activateCompressionAudio=()=>Promise.resolve(true);
playCompressionCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:4,events:[]};await setCompressionSoundEnabled(true);
assert.equal(plays,0);assert.equal(storage[COMPRESSION_ALERT_CURSOR_KEY],'4');
fetchPayload={latest_alert_id:6,events:[{alert_id:5},{alert_id:6}]};await pollCompressionAlerts();
assert.equal(plays,1);assert.equal(storage[COMPRESSION_ALERT_CURSOR_KEY],'6');process.stdout.write('ok');})()
"""
        self.assertEqual(run_compression_sound_javascript(body), "ok")


if __name__ == "__main__":
    unittest.main()
