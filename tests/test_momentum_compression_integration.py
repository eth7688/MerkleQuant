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

    def test_status_maps_alert_and_delivery_facts_to_pool_and_terminal_rows(self):
        self._login()
        current = {"compression_id": "current", "symbol": "CURRENTUSDT", "side": "LONG", "state": "BREAKOUT_ACTIVE_LONG", "htf_alignment": "CONFIRMED"}
        terminal = {"compression_id": "terminal", "symbol": "TERMINALUSDT", "side": "SHORT", "state": "BREAKOUT_FAILED", "htf_alignment": "CONFIRMED"}
        with patch.object(web_ui, "load_compression_state", return_value={
            "pool": {"current": current},
            "episodes": {"terminal": terminal},
            "last_scan_failures": [{
                "symbol": "BADUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
                "message": "upstream <slow>", "attempts": 2,
            }],
        }), \
             patch.object(web_ui._compression_monitor, "status", return_value={"running": False}), \
             patch.object(web_ui, "compression_sound_available_ids", return_value={"current"}), \
             patch.object(web_ui, "compression_delivery_statuses", return_value={"current": "delivered", "terminal": "failed"}):
            payload = self.client.get("/api/compression/status").get_json()
        self.assertEqual(payload["pool_rows"][0]["sound_status"], "available")
        self.assertEqual(payload["pool_rows"][0]["wechat_status"], "delivered")
        self.assertEqual(payload["episode_rows"][0]["sound_status"], "unknown")
        self.assertEqual(payload["episode_rows"][0]["wechat_status"], "failed")
        self.assertEqual(payload["scan_failures"], [{
            "symbol": "BADUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
            "message": "upstream <slow>", "attempts": 2,
        }])

    def test_status_uses_persisted_failures_for_scan_error_count(self):
        self._login()
        failures = [{
            "symbol": "BADUSDT", "stage": "15m_klines", "error_type": "TimeoutError",
            "message": "upstream slow", "attempts": 2,
        }]
        with patch.object(web_ui, "load_compression_state", return_value={
            "pool": {}, "episodes": {}, "last_scan_failures": failures,
        }), \
             patch.object(web_ui._compression_monitor, "status", return_value={"running": False}), \
             patch.object(web_ui, "_compression_scan_summary", {"scanned": 7, "eligible": 2, "errors": 0}), \
             patch.object(web_ui, "compression_sound_available_ids", return_value=set()), \
             patch.object(web_ui, "compression_delivery_statuses", return_value={}):
            payload = self.client.get("/api/compression/status").get_json()

        self.assertEqual(payload["scan_failures"], failures)
        self.assertEqual(payload["scan"]["errors"], len(payload["scan_failures"]))

    def test_status_marks_unknown_conflict_and_nonconfirmed_rows_not_eligible_for_wechat(self):
        self._login()
        pool = {
            "unknown": {"compression_id": "unknown", "symbol": "UNKNOWNUSDT", "side": "LONG", "state": "BREAKOUT_ACTIVE_LONG", "htf_alignment": "UNKNOWN"},
            "conflict": {"compression_id": "conflict", "symbol": "CONFLICTUSDT", "side": "LONG", "state": "BREAKOUT_ACTIVE_LONG", "htf_alignment": "CONFLICT"},
            "missing": {"compression_id": "missing", "symbol": "MISSINGUSDT", "side": "LONG", "state": "BREAKOUT_ACTIVE_LONG"},
        }
        with patch.object(web_ui, "load_compression_state", return_value={"pool": pool, "episodes": {}}), \
             patch.object(web_ui._compression_monitor, "status", return_value={"running": False}), \
             patch.object(web_ui, "compression_sound_available_ids", return_value=set()), \
             patch.object(web_ui, "compression_delivery_statuses", return_value={}):
            payload = self.client.get("/api/compression/status").get_json()

        self.assertEqual({row["wechat_status"] for row in payload["pool_rows"]}, {"not_eligible"})

    def test_only_15m_manual_compression_scan_is_valid(self):
        self.assertEqual(self.client.get("/scan/compression/15m").status_code, 401)
        self._login()
        self.assertEqual(self.client.get("/scan/compression/1h").status_code, 400)
        with patch.object(web_ui._compression_monitor, "scan_now", return_value=True) as scan_now, \
             patch.object(web_ui.threading, "Thread") as worker:
            response = self.client.get("/scan/compression/15m")
        self.assertEqual(response.status_code, 200)
        worker.assert_called_once()
        scan_now.assert_not_called()
        if web_ui._compression_manual_scan_lock.locked():
            web_ui._compression_manual_scan_lock.release()

    def test_overlapping_manual_scans_start_only_one_monitor_scan(self):
        started, release = threading.Event(), threading.Event()
        def scan_now(trigger):
            started.set(); release.wait(1); return True
        self._login()
        with patch.object(web_ui._compression_monitor, "scan_now", side_effect=scan_now) as scan:
            first = self.client.get("/scan/compression/15m")
            self.assertEqual(first.status_code, 200)
            self.assertTrue(started.wait(1))
            second = self.client.get("/scan/compression/15m")
            release.set()
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
        with patch.object(web_ui, "drain_compression_outbox", return_value=[dict(_fresh_event())]) as append:
            web_ui._compression_alert_wakeup.clear()
            created = web_ui._process_compression_events([_fresh_event()], 11)
        self.assertEqual(len(created), 1)
        append.assert_called_once()
        self.assertTrue(web_ui._compression_alert_wakeup.is_set())

    def test_unconfirmed_events_do_not_wake_delivery_worker(self):
        with patch.object(web_ui, "drain_compression_outbox", return_value=[_fresh_event("UNKNOWN")]):
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
const assert=require('assert');let storage={},fetchPayload={latest_alert_id:0,events:[]},fetches=[];
global.localStorage={getItem:k=>storage[k]??null,setItem:(k,v)=>storage[k]=String(v)};
global.window=global;global.document={getElementById:()=>null};
global.fetch=url=>{fetches.push(url);return Promise.resolve({ok:true,json:()=>Promise.resolve(fetchPayload)});};
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
        self.assertIn("数据源：Binance Futures", source)

    def test_renderer_escapes_payload_and_formats_nonfinite_values(self):
        result = render_compression_payload({
            "monitor": {"auto_enabled": True, "running": True, "last_scan_at": 0, "next_scan_at": "2026-08-21T12:15:05+00:00", "last_error": "<b>stream failed</b>", "today_fresh": 1, "dropped_price_rows": 7},
            "scan": {"scanned": 200, "eligible": 12, "errors": 3},
            "alert": {"last_delivery_at": 1720000000000, "last_error": "<i>wechat</i>"},
            "can_manage": False,
            "pool_rows": [
                {"symbol": "<script>alert(1)</script>", "side": "LONG", "state": "PRE_BREAKOUT", "live_price": float("nan"), "compression_bars": float("inf")},
                {"symbol": "FRESHUSDT", "side": "SHORT", "state": "BREAKOUT_ACTIVE_SHORT", "live_price": 12.5, "breakout_at": 1720000000000, "breakout_price": 11.9, "sound_status": "available", "wechat_status": "delivered", "quality_score": 98, "upper_boundary_price": 13, "lower_boundary_price": 12, "breakout_buffer_price": 11.95, "directional_touch_count": 4, "contraction_ratio": 0.5, "ema8": 12.6, "ema21": 12.4, "atr14": 0.2, "compression_bars": 22, "htf_alignment": "CONFIRMED", "first_seen_at": 1720000000000, "last_price_at": 1720000001000, "last_verified_at": 1720000002000},
                {"symbol": "LOWUSDT", "side": "SHORT", "state": "COMPRESSION_ACTIVE_SHORT", "live_price": 12.1, "quality_score": 10},
                {"symbol": "WAITUSDT", "side": "LONG", "state": "BREAKOUT_UNCONFIRMED_LONG", "live_price": 11.1, "quality_score": 99},
            ],
            "episode_rows": [{"symbol": "FAILEDUSDT", "side": "SHORT", "state": "BREAKOUT_FAILED", "live_price": 12.5, "breakout_at": 1720000000000, "breakout_price": 11.9, "sound_status": "unknown", "wechat_status": "failed", "quality_score": 20}],
            "rejection_counts": {"<img src=x>": float("inf")},
            "scan_failures": [{
                "symbol": "<failure-symbol>", "stage": "<failure-stage>",
                "error_type": "<failure-type>", "message": "<failure-message>", "attempts": 2,
            }],
        })
        rendered = result["stats"]["innerHTML"] + result["main"]["innerHTML"]
        self.assertIn("数据源：Binance Futures", rendered)
        self.assertIn("LONG 观察池", rendered)
        self.assertIn("SHORT 观察池", rendered)
        self.assertIn("当前突破", rendered)
        self.assertIn("终止结构", rendered)
        self.assertIn("拒绝统计", rendered)
        self.assertIn("微信错误", rendered)
        self.assertNotIn("本轮新突破", rendered)
        for label in ("压缩K线", "大周期", "入池时间", "边界距离", "触碰", "收敛", "EMA8/21", "ATR", "质量", "下次扫描", "监控错误", "已扫描", "合格候选", "错误数", "当前已触发结构数", "丢弃价格行", "突破时间", "突破价格", "最新价格", "最新时间", "声音", "微信", "缓冲", "声音提醒", "微信投递"):
            self.assertIn(label, rendered)
        for value in (">200<", ">12<", ">3<", ">7<", ">22<", "CONFIRMED", "11.900000", "12.500000", "11.950000", "2.50 ATR", "已投递", "投递失败"):
            self.assertIn(value, rendered)
        self.assertLess(rendered.index("FRESH"), rendered.index("LOW"))
        breakout_section = rendered[rendered.index("当前突破"):rendered.index("终止结构")]
        self.assertIn("FRESH", breakout_section)
        self.assertIn("BREAKOUT_ACTIVE_SHORT", breakout_section)
        self.assertNotIn("WAIT", breakout_section)
        self.assertIn("仅管理员可修改", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;i&gt;wechat&lt;/i&gt;", rendered)
        self.assertIn("FRESHUSDT", rendered)
        self.assertIn("扫描失败明细", rendered)
        for escaped in ("&lt;failure-symbol&gt;", "&lt;failure-stage&gt;", "&lt;failure-type&gt;", "&lt;failure-message&gt;"):
            self.assertIn(escaped, rendered)
        for raw in ("<failure-symbol>", "<failure-stage>", "<failure-type>", "<failure-message>"):
            self.assertNotIn(raw, rendered)
        self.assertIn("<td>2</td>", rendered)
        self.assertNotRegex(rendered, r"NaN|Infinity")

    def test_rejection_counts_are_sorted_by_count_then_name(self):
        rendered = render_compression_payload({
            "rejection_counts": {
                "A_LOW": 1,
                "Z_HIGH": 5,
                "B_MIDDLE": 3,
                "A_HIGH": 5,
            },
        })["main"]["innerHTML"]
        rejection_section = rendered[rendered.index("拒绝统计"):]

        self.assertLess(rejection_section.index("A_HIGH"), rejection_section.index("Z_HIGH"))
        self.assertLess(rejection_section.index("Z_HIGH"), rejection_section.index("B_MIDDLE"))
        self.assertLess(rejection_section.index("B_MIDDLE"), rejection_section.index("A_LOW"))

    def test_renderer_retains_full_symbols_in_each_compression_table(self):
        rendered = render_compression_payload({
            "pool_rows": [
                {"symbol": "POOLONLYUSDT", "side": "LONG", "state": "PRE_BREAKOUT"},
                {"symbol": "CURRENTONLYUSDT", "side": "SHORT", "state": "BREAKOUT_ACTIVE_SHORT"},
            ],
            "episode_rows": [{"symbol": "TERMINALONLYUSDT", "side": "SHORT", "state": "BREAKOUT_FAILED"}],
        })["main"]["innerHTML"]

        pool_section = rendered[rendered.index("LONG 观察池"):rendered.index("SHORT 观察池")]
        current_section = rendered[rendered.index("当前突破"):rendered.index("终止结构")]
        terminal_section = rendered[rendered.index("终止结构"):rendered.index("拒绝统计")]
        self.assertIn("POOLONLYUSDT", pool_section)
        self.assertIn("CURRENTONLYUSDT", current_section)
        self.assertIn("TERMINALONLYUSDT", terminal_section)

    def test_renderer_omits_failure_details_without_a_failure_list(self):
        for scan_failures in ([], {"symbol": "not-a-list"}):
            rendered = render_compression_payload({"scan_failures": scan_failures})["main"]["innerHTML"]
            self.assertNotIn("扫描失败明细", rendered)

    def test_renderer_gives_each_wechat_delivery_state_a_distinct_chinese_label(self):
        statuses = ("not_eligible", "pending", "delivered", "failed", "indeterminate", "unknown")
        rows = [
            {"symbol": f"{index}USDT", "side": "LONG", "state": "BREAKOUT_ACTIVE_LONG",
             "wechat_status": status}
            for index, status in enumerate(statuses, 1)
        ]
        result = render_compression_payload({"pool_rows": rows, "episode_rows": []})
        rendered = result["main"]["innerHTML"]

        for label in ("不符合投递条件", "待投递", "已投递", "投递失败", "投递结果待确认", "无投递记录"):
            self.assertIn(label, rendered)

    def test_sound_enable_baselines_then_plays_once_for_new_batch(self):
        body = """
let plays=0;activateCompressionAudio=()=>Promise.resolve(true);
playCompressionCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:4,events:[]};await setCompressionSoundEnabled(true);
assert.equal(plays,0);assert.equal(storage[COMPRESSION_ALERT_CURSOR_KEY],'4');
fetchPayload={latest_alert_id:6,events:[{alert_id:5},{alert_id:6}]};await pollCompressionAlerts();
assert.deepEqual(fetches,['/api/compression/alerts?after=0','/api/compression/alerts?after=4']);
assert.equal(plays,1);assert.equal(storage[COMPRESSION_ALERT_CURSOR_KEY],'6');process.stdout.write('ok');})()
"""
        self.assertEqual(run_compression_sound_javascript(body), "ok")

    def test_stale_baseline_settlement_cannot_clear_new_generation_guard(self):
        body = """
let pending=[],plays=0;global.fetch=url=>{fetches.push(url);return new Promise(resolve=>pending.push({url,resolve}));};
activateCompressionAudio=()=>Promise.resolve(true);playCompressionCoinSound=()=>{plays++;return Promise.resolve(true);};
;(async()=>{let first=setCompressionSoundEnabled(true);await new Promise(resolve=>setImmediate(resolve));
await setCompressionSoundEnabled(false);let second=setCompressionSoundEnabled(true);await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(fetches,['/api/compression/alerts?after=0','/api/compression/alerts?after=0']);
pending[0].resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:4,events:[]})});await first;
assert.ok(_compressionAlertBaselinePromise);pending[1].resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:4,events:[]})});await second;
fetchPayload={latest_alert_id:5,events:[{alert_id:5}]};global.fetch=url=>{fetches.push(url);return Promise.resolve({ok:true,json:()=>Promise.resolve(fetchPayload)});};
await pollCompressionAlerts();assert.equal(plays,1);assert.equal(storage[COMPRESSION_ALERT_CURSOR_KEY],'5');process.stdout.write('ok');})()
"""
        self.assertEqual(run_compression_sound_javascript(body), "ok")

    def test_manual_compression_scan_uses_status_not_global_scan_polling(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")
        scan = source[source.index("function doScan()") : source.index("function pollResults()")]
        self.assertIn("startCompressionManualScan()", scan)
        self.assertLess(scan.index("startCompressionManualScan()"), scan.index("_pollingScan"))
        self.assertIn("structure_scanning", source)


if __name__ == "__main__":
    unittest.main()
