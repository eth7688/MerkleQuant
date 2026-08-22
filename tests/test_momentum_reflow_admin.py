import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import admin_server
import momentum_reflow_alerts as alerts
from momentum_reflow_alerts import observe_reflow_alerts
from momentum_reflow_dashboard import load_reflow_settings, save_reflow_settings


def login_admin(client):
    with client.session_transaction() as admin_session:
        admin_session["admin_id"] = 7
        admin_session["username"] = "admin"
        admin_session["role"] = "admin"


def run_admin_javascript(test_body):
    javascript = admin_server.ADMIN_HTML.split("<script>", 1)[1].split(
        "</script>", 1
    )[0]
    harness = textwrap.dedent(
        """
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');
        vm.runInThisContext(fs.readFileSync(0, 'utf8'));
        vm.runInThisContext("_currentPage='reflow'");

        const ids = [
          'reflowAutoEnabled', 'reflowEnabledState', 'reflowUpdatedAt',
          'reflowUpdatedBy', 'reflowLastScan', 'reflowNextScan',
          'reflowLastError', 'reflowSaveError',
          'reflowWechatEnabled', 'reflowWebhook', 'reflowWebhookMask',
          'reflowWechatState', 'reflowWechatTest', 'reflowWechatTestState',
          'reflowWechatDeliveryState', 'reflowAlertSaveError'
        ];
        let elements = {};
        function newElement(id) {
          return {
            id:id, checked:false, disabled:false, dataset:{},
            style:{display:'none'}, textContent:'', value:'', onchange:null
          };
        }
        function replacePanelElements() {
          elements = {};
          ids.forEach(function(id){ elements[id] = newElement(id); });
        }
        const content = {};
        Object.defineProperty(content, 'innerHTML', {
          set:function(){ replacePanelElements(); }
        });
        replacePanelElements();
        global.document = {
          getElementById:function(id){ return id === 'content' ? content : elements[id]; },
          querySelectorAll:function(){ return []; }
        };
        function deferred() {
          let resolve, reject;
          const promise = new Promise(function(ok, fail){ resolve=ok; reject=fail; });
          return {promise:promise, resolve:resolve, reject:reject};
        }
        function response(ok, data) {
          return {ok:ok, json:function(){ return Promise.resolve(data); }};
        }
        async function flush() {
          await Promise.resolve();
          await Promise.resolve();
          await new Promise(function(resolve){ setImmediate(resolve); });
        }
        const requests = [];
        global.fetch = function(){
          const request = deferred();
          request.args = Array.prototype.slice.call(arguments);
          requests.push(request);
          return request.promise;
        };

        (async function(){
        """
    )
    footer = textwrap.dedent(
        """
        })().catch(function(error){
          console.error(error && error.stack ? error.stack : error);
          process.exitCode = 1;
        });
        """
    )
    process = subprocess.run(
        ["node", "-e", harness + test_body + footer],
        input=javascript,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr or process.stdout)


class MomentumReflowAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "momentum_reflow_settings.json"
        self.path_patch = patch.object(
            admin_server, "_REFLOW_SETTINGS_PATH", self.path, create=True
        )
        self.path_patch.start()
        admin_server.app.config.update(TESTING=True)
        self.client = admin_server.app.test_client()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_unauthenticated_get_and_post_are_rejected(self):
        self.assertEqual(self.client.get("/api/reflow/settings").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/reflow/settings", json={"auto_scan_enabled": False}
            ).status_code,
            403,
        )

    def test_role_only_session_get_and_post_are_rejected_without_creating_file(self):
        with self.client.session_transaction() as admin_session:
            admin_session["username"] = "admin"
            admin_session["role"] = "admin"

        self.assertEqual(self.client.get("/api/reflow/settings").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/reflow/settings", json={"auto_scan_enabled": False}
            ).status_code,
            403,
        )
        self.assertFalse(self.path.exists())

    def test_role_only_session_cannot_change_existing_settings(self):
        save_reflow_settings(self.path, True, "operator", 123)
        original = self.path.read_bytes()
        with self.client.session_transaction() as admin_session:
            admin_session["username"] = "admin"
            admin_session["role"] = "admin"

        response = self.client.post(
            "/api/reflow/settings", json={"auto_scan_enabled": False}
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.path.read_bytes(), original)

    def test_admin_can_disable_and_persist_switch(self):
        login_admin(self.client)

        response = self.client.post(
            "/api/reflow/settings",
            json={"auto_scan_enabled": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(load_reflow_settings(self.path)["auto_scan_enabled"])
        self.assertEqual(load_reflow_settings(self.path)["updated_by"], "7")

    def test_post_rejects_non_boolean_value(self):
        login_admin(self.client)

        response = self.client.post(
            "/api/reflow/settings",
            json={"auto_scan_enabled": "false"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())

    def test_get_combines_persisted_settings_with_scheduler_status(self):
        save_reflow_settings(self.path, False, "operator", 123)
        login_admin(self.client)
        scheduler_response = Mock()
        scheduler_response.raise_for_status.return_value = None
        scheduler_response.json.return_value = {
            "auto_scan_enabled": True,
            "last_auto_scan_at": 456,
            "next_scan_at": 789,
            "last_auto_error": "recent error",
            "running": True,
        }

        with patch.object(
            admin_server._requests, "get", return_value=scheduler_response
        ) as status_get:
            response = self.client.get("/api/reflow/settings")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["auto_scan_enabled"])
        self.assertEqual(payload["updated_at"], 123)
        self.assertEqual(payload["updated_by"], "operator")
        self.assertEqual(payload["last_auto_scan_at"], 456)
        self.assertEqual(payload["next_scan_at"], 789)
        self.assertEqual(payload["last_auto_error"], "recent error")
        self.assertEqual(payload["scheduler_status"], "available")
        self.assertTrue(payload["running"])
        status_get.assert_called_once_with(
            "http://127.0.0.1:5000/api/reflow/automation/status",
            timeout=3,
        )

    def test_status_proxy_failure_preserves_persisted_switch(self):
        save_reflow_settings(self.path, True, "operator", 123)
        original = self.path.read_bytes()
        login_admin(self.client)

        with patch.object(
            admin_server._requests, "get", side_effect=RuntimeError("offline")
        ):
            response = self.client.get("/api/reflow/settings")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["auto_scan_enabled"])
        self.assertEqual(response.get_json()["scheduler_status"], "unavailable")
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_scheduler_fields_make_status_unavailable(self):
        invalid_fields = (
            ("last_auto_scan_at", -1),
            ("last_auto_scan_at", float("nan")),
            ("last_auto_scan_at", float("inf")),
            ("last_auto_scan_at", True),
            ("last_auto_scan_at", "456"),
            ("next_scan_at", -1),
            ("next_scan_at", None),
            ("last_auto_error", 7),
            ("scheduler_status", False),
            ("running", "true"),
        )
        save_reflow_settings(self.path, True, "operator", 123)
        original = self.path.read_bytes()
        login_admin(self.client)

        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                scheduler_response = Mock()
                scheduler_response.raise_for_status.return_value = None
                scheduler_response.json.return_value = {
                    "last_auto_scan_at": 456,
                    "next_scan_at": 789,
                    "last_auto_error": "",
                    "running": True,
                    field: value,
                }
                with patch.object(
                    admin_server._requests, "get", return_value=scheduler_response
                ):
                    response = self.client.get("/api/reflow/settings")

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["auto_scan_enabled"])
                self.assertEqual(
                    response.get_json()["scheduler_status"], "unavailable"
                )
                self.assertEqual(self.path.read_bytes(), original)

    def test_post_only_writes_reflow_settings_file(self):
        demo_config = Path(self.temp_dir.name) / "demo_bot_config.json"
        user_config = Path(self.temp_dir.name) / "user_configs.json"
        demo_config.write_text('{"api_key":"demo-secret"}', encoding="utf-8")
        user_config.write_text('{"api_secret":"user-secret"}', encoding="utf-8")
        before = {
            demo_config: demo_config.read_bytes(),
            user_config: user_config.read_bytes(),
        }
        login_admin(self.client)

        response = self.client.post(
            "/api/reflow/settings",
            json={
                "auto_scan_enabled": False,
                "min_volume_usdt": 1,
                "api_key": "replacement",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(load_reflow_settings(self.path)),
            {"version", "auto_scan_enabled", "updated_at", "updated_by"},
        )
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_admin_html_exposes_reflow_navigation_and_status_fields(self):
        html = admin_server.ADMIN_HTML

        self.assertIn('data-page="reflow"', html)
        self.assertIn("动能回流", html)
        for element_id in (
            "reflowAutoEnabled",
            "reflowEnabledState",
            "reflowUpdatedAt",
            "reflowUpdatedBy",
            "reflowLastScan",
            "reflowNextScan",
            "reflowLastError",
            "reflowSaveError",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("50万 USDT", html)
        self.assertIn("不会删除历史记录", html)
        self.assertIn("手动扫描仍可使用", html)

    def test_reflow_toggle_posts_boolean_and_rolls_back_on_failure(self):
        html = admin_server.ADMIN_HTML

        self.assertIn(
            "body:JSON.stringify({auto_scan_enabled:Boolean(box.checked)})",
            html,
        )
        self.assertIn("box.onchange=saveReflowSetting", html)
        self.assertIn("box.checked=previous", html)
        self.assertIn("error.textContent=", html)
        self.assertNotIn(
            'id="reflowAutoEnabled" onclick=',
            html,
        )

    def test_stale_get_does_not_mutate_replacement_panel(self):
        run_admin_javascript(
            textwrap.dedent(
                """
                  renderReflow(content);
                  const detachedBox = elements.reflowAutoEnabled;
                  renderReflow(content);
                  const currentBox = elements.reflowAutoEnabled;
                  const currentState = elements.reflowEnabledState;

                  requests[0].resolve(response(true, {
                    auto_scan_enabled:true, updated_at:100, updated_by:'old',
                    last_auto_scan_at:200, next_scan_at:300,
                    last_auto_error:'stale', scheduler_status:'available'
                  }));
                  await flush();

                  assert.strictEqual(detachedBox.disabled, true);
                  assert.strictEqual(detachedBox.onchange, null);
                  assert.strictEqual(currentBox.disabled, true);
                  assert.strictEqual(currentState.textContent, '');

                  requests[2].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:400, updated_by:'current',
                    last_auto_scan_at:500, next_scan_at:600,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  assert.strictEqual(currentBox.disabled, false);
                  assert.strictEqual(currentBox.checked, false);
                  assert.strictEqual(currentState.textContent, '已关闭');
                  assert.strictEqual(typeof currentBox.onchange, 'function');
                """
            )
        )

    def test_stale_failed_save_is_ignored_and_current_failure_rolls_back(self):
        run_admin_javascript(
            textwrap.dedent(
                """
                  renderReflow(content);
                  requests[0].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  const detachedBox = elements.reflowAutoEnabled;
                  const detachedError = elements.reflowSaveError;
                  detachedBox.checked = true;
                  detachedBox.onchange();
                  assert.strictEqual(detachedBox.disabled, true);

                  renderReflow(content);
                  const currentBox = elements.reflowAutoEnabled;
                  const currentError = elements.reflowSaveError;
                  requests[2].resolve(response(false, {error:'stale failure'}));
                  await flush();

                  assert.strictEqual(detachedBox.checked, true);
                  assert.strictEqual(detachedBox.disabled, true);
                  assert.strictEqual(detachedError.style.display, 'none');
                  assert.strictEqual(detachedError.textContent, '');
                  assert.strictEqual(currentBox.disabled, true);
                  assert.strictEqual(currentError.style.display, 'none');

                  requests[3].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:300, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:400,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  currentBox.checked = true;
                  currentBox.onchange();
                  requests[5].resolve(response(false, {error:'current failure'}));
                  await flush();

                  assert.strictEqual(currentBox.checked, false);
                  assert.strictEqual(currentBox.disabled, false);
                  assert.strictEqual(currentError.style.display, 'block');
                  assert.strictEqual(currentError.textContent, 'current failure');
                """
            )
        )

    def test_stale_success_refreshes_current_panel_after_old_snapshot(self):
        run_admin_javascript(
            textwrap.dedent(
                """
                  renderReflow(content);
                  requests[0].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  const detachedBox = elements.reflowAutoEnabled;
                  detachedBox.checked = true;
                  detachedBox.onchange();

                  renderReflow(content);
                  const oldSnapshotBox = elements.reflowAutoEnabled;
                  requests[3].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  assert.strictEqual(oldSnapshotBox.checked, false);

                  requests[2].resolve(response(true, {
                    ok:true, auto_scan_enabled:true,
                    updated_at:300, updated_by:'admin'
                  }));
                  await flush();

                  assert.strictEqual(requests.length, 7);
                  assert.strictEqual(detachedBox.checked, true);
                  assert.strictEqual(detachedBox.disabled, true);
                  assert.strictEqual(detachedBox.onchange instanceof Function, true);
                  const refreshedBox = elements.reflowAutoEnabled;
                  const refreshedState = elements.reflowEnabledState;
                  assert.notStrictEqual(refreshedBox, oldSnapshotBox);
                  requests[5].resolve(response(true, {
                    auto_scan_enabled:true, updated_at:300, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:400,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();

                  assert.strictEqual(refreshedBox.checked, true);
                  assert.strictEqual(refreshedBox.disabled, false);
                  assert.strictEqual(refreshedState.textContent, '已启用');
                  await flush();
                  assert.strictEqual(requests.length, 7);
                """
            )
        )

    def test_overlapping_saves_converge_after_last_completed_save(self):
        run_admin_javascript(
            textwrap.dedent(
                """
                  renderReflow(content);
                  requests[0].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  const firstBox = elements.reflowAutoEnabled;
                  firstBox.checked = true;
                  firstBox.onchange();

                  renderReflow(content);
                  requests[3].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  const secondBox = elements.reflowAutoEnabled;
                  const secondError = elements.reflowSaveError;
                  secondBox.checked = false;
                  secondBox.onchange();

                  requests[2].resolve(response(true, {
                    ok:true, auto_scan_enabled:true,
                    updated_at:300, updated_by:'admin'
                  }));
                  await flush();
                  assert.strictEqual(requests.length, 8);
                  const interimBox = elements.reflowAutoEnabled;
                  requests[6].resolve(response(true, {
                    auto_scan_enabled:true, updated_at:300, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:400,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  assert.strictEqual(interimBox.checked, true);

                  requests[5].resolve(response(true, {
                    ok:true, auto_scan_enabled:false,
                    updated_at:500, updated_by:'admin'
                  }));
                  await flush();
                  assert.strictEqual(requests.length, 10);
                  assert.strictEqual(secondBox.checked, false);
                  assert.strictEqual(secondBox.disabled, true);
                  assert.strictEqual(secondError.style.display, 'none');
                  const finalBox = elements.reflowAutoEnabled;
                  const finalState = elements.reflowEnabledState;
                  requests[8].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:500, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:600,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();

                  assert.strictEqual(finalBox.checked, false);
                  assert.strictEqual(finalBox.disabled, false);
                  assert.strictEqual(finalState.textContent, '已关闭');
                  await flush();
                  assert.strictEqual(requests.length, 10);
                """
            )
        )

    def test_navigation_away_blocks_pending_save_reconciliation(self):
        run_admin_javascript(
            textwrap.dedent(
                """
                  renderReflow(content);
                  requests[0].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  const detachedBox = elements.reflowAutoEnabled;
                  const detachedState = elements.reflowEnabledState;
                  const detachedError = elements.reflowSaveError;
                  detachedBox.checked = true;
                  detachedBox.onchange();

                  switchPage('dashboard');
                  assert.strictEqual(requests.length, 4);
                  assert.strictEqual(elements.reflowAutoEnabled, detachedBox);
                  requests[2].resolve(response(true, {
                    ok:true, auto_scan_enabled:true,
                    updated_at:300, updated_by:'admin'
                  }));
                  await flush();

                  assert.strictEqual(requests.length, 4);
                  assert.strictEqual(elements.reflowAutoEnabled, detachedBox);
                  assert.strictEqual(detachedBox.checked, true);
                  assert.strictEqual(detachedBox.disabled, true);
                  assert.strictEqual(detachedBox.dataset.savedChecked, 'false');
                  assert.strictEqual(detachedState.textContent, '已关闭');
                  assert.strictEqual(detachedError.style.display, 'none');
                  assert.strictEqual(detachedError.textContent, '');
                """
            )
        )


class CompressionAdminTests(unittest.TestCase):
    def setUp(self):
        admin_server.app.config.update(TESTING=True)
        self.client = admin_server.app.test_client()

    def test_compression_settings_require_authenticated_admin(self):
        self.assertEqual(self.client.get("/api/compression/settings").status_code, 403)
        self.assertEqual(
            self.client.post("/api/compression/settings", json={"enabled": True}).status_code,
            403,
        )

    def test_compression_settings_get_whitelists_internal_facts(self):
        login_admin(self.client)
        internal = Mock()
        internal.raise_for_status.return_value = None
        internal.json.return_value = {
            "monitor": {
                "auto_enabled": True,
                "running": True,
                "last_scan_at": 10,
                "next_scan_at": "2026-08-22T00:15:00+00:00",
                "structure_scanning": False,
                "scan_started_at": 5,
                "scan_duration_ms": 123,
                "last_error": "",
                "secret": "must not leak",
            },
            "scan": {"scanned": 9, "eligible": 2, "errors": 1, "extra": "no"},
            "alert": {"webhook": "must not leak"},
        }
        with patch.object(admin_server._requests, "get", return_value=internal) as get:
            response = self.client.get("/api/compression/settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "monitor": {
                    "auto_enabled": True,
                    "running": True,
                    "last_scan_at": 10,
                    "next_scan_at": "2026-08-22T00:15:00+00:00",
                    "structure_scanning": False,
                    "scan_started_at": 5,
                    "scan_duration_ms": 123,
                    "last_error": "",
                },
                "scan": {"scanned": 9, "eligible": 2, "errors": 1},
            },
        )
        get.assert_called_once_with(
            "http://127.0.0.1:5000/internal/compression/status", timeout=3
        )

    def test_compression_settings_post_forwards_exact_boolean(self):
        login_admin(self.client)
        internal = Mock()
        internal.raise_for_status.return_value = None
        internal.json.return_value = {"auto_enabled": True}
        with patch.object(admin_server._requests, "post", return_value=internal) as post:
            response = self.client.post("/api/compression/settings", json={"enabled": True})

        self.assertEqual(response.status_code, 200)
        post.assert_called_once_with(
            "http://127.0.0.1:5000/internal/compression/automation",
            json={"enabled": True},
            timeout=3,
        )

    def test_compression_settings_post_whitelists_internal_response(self):
        login_admin(self.client)
        internal = Mock()
        internal.raise_for_status.return_value = None
        internal.json.return_value = {
            "running": True,
            "auto_enabled": True,
            "last_scan_at": 10,
            "next_scan_at": "2026-08-22T00:15:00+00:00",
            "structure_scanning": False,
            "scan_started_at": 5,
            "scan_duration_ms": 123,
            "last_error": "",
            "secret": "must not leak",
        }
        with patch.object(admin_server._requests, "post", return_value=internal):
            response = self.client.post("/api/compression/settings", json={"enabled": True})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "monitor": {
                    "running": True,
                    "auto_enabled": True,
                    "last_scan_at": 10,
                    "next_scan_at": "2026-08-22T00:15:00+00:00",
                    "structure_scanning": False,
                    "scan_started_at": 5,
                    "scan_duration_ms": 123,
                    "last_error": "",
                }
            },
        )

    def test_compression_html_exposes_navigation_and_renderer(self):
        html = admin_server.ADMIN_HTML

        self.assertIn('data-page="compression"', html)
        self.assertIn("renderCompression", html)
        self.assertIn("compressionAutoEnabled", html)
        self.assertIn("compressionSaveError", html)


class MomentumReflowAlertAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = root / "alert_settings.json"
        self.ledger = root / "alerts.json"
        self.patches = [
            patch.object(
                admin_server, "_REFLOW_ALERT_SETTINGS_PATH", self.settings, create=True
            ),
            patch.object(
                admin_server, "_REFLOW_ALERT_LEDGER_PATH", self.ledger, create=True
            ),
        ]
        for item in self.patches:
            item.start()
        admin_server.app.config.update(TESTING=True)
        self.client = admin_server.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_alert_settings_require_admin(self):
        self.assertEqual(
            self.client.get("/api/reflow/alert-settings").status_code, 403
        )
        self.assertEqual(
            self.client.post("/api/reflow/alert-settings", json={}).status_code, 403
        )
        self.assertEqual(
            self.client.post("/api/reflow/alert-settings/test").status_code, 403
        )

    def test_admin_saves_secret_but_response_only_contains_mask(self):
        login_admin(self.client)
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake-secret-1234"

        response = self.client.post(
            "/api/reflow/alert-settings",
            json={"wechat_enabled": False, "wechat_webhook": url},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            url in response.get_data(as_text=True),
            "response leaked configured webhook",
        )
        self.assertNotIn("wechat_webhook", response.get_json())
        self.assertEqual(response.get_json()["webhook_mask"], "****1234")
        self.assertEqual(
            admin_server.load_alert_settings(self.settings)["wechat_webhook"], url
        )

    def test_blank_input_preserves_saved_secret(self):
        login_admin(self.client)
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake-secret-1234"
        first = self.client.post(
            "/api/reflow/alert-settings",
            json={"wechat_enabled": False, "wechat_webhook": url},
        )
        self.assertEqual(first.status_code, 200)

        response = self.client.post(
            "/api/reflow/alert-settings",
            json={"wechat_enabled": False, "wechat_webhook": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["webhook_mask"], "****1234")
        self.assertEqual(
            admin_server.load_alert_settings(self.settings)["wechat_webhook"], url
        )

    def test_enabling_without_webhook_is_rejected(self):
        login_admin(self.client)

        response = self.client.post(
            "/api/reflow/alert-settings",
            json={"wechat_enabled": True, "wechat_webhook": ""},
        )

        self.assertEqual(response.status_code, 400)

    def test_enable_transition_atomically_baselines_existing_events(self):
        login_admin(self.client)
        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
            "fake-secret-1234"
        )
        saved = self.client.post(
            "/api/reflow/alert-settings",
            json={"wechat_enabled": False, "wechat_webhook": url},
        )
        self.assertEqual(saved.status_code, 200)
        observe_reflow_alerts(self.ledger, [], 1)
        observe_reflow_alerts(self.ledger, [{
            "signal_key": "historical", "symbol": "BTCUSDT",
            "direction": "LONG", "quality_label": "HIGH", "status": "ACTIVE",
            "price": 100.0, "ema50": 99.0, "daily_kind": "strong_momentum",
            "window_index": 1, "breakout_volume_ratio": 3.0,
            "first_seen_at": 100,
        }], 2)

        with patch.object(
            admin_server,
            "enable_wechat_alerts",
            wraps=alerts.enable_wechat_alerts,
            create=True,
        ) as enable:
            response = self.client.post(
                "/api/reflow/alert-settings",
                json={"wechat_enabled": True, "wechat_webhook": ""},
            )

        self.assertEqual(response.status_code, 200)
        enable.assert_called_once()
        self.assertEqual(response.get_json()["webhook_mask"], "****1234")
        self.assertTrue(
            admin_server.load_alert_settings(self.settings)["wechat_enabled"]
        )
        ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(ledger["wechat_cursor"], 1)
        self.assertEqual(len(ledger["events"]), 1)

    def test_invalid_webhook_is_rejected_before_baseline(self):
        login_admin(self.client)
        response = self.client.post(
            "/api/reflow/alert-settings",
            json={"wechat_enabled": True, "wechat_webhook": "https://invalid.test"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.ledger.exists())
        saved = admin_server.load_alert_settings(self.settings)
        self.assertFalse(saved["wechat_enabled"])
        self.assertEqual(saved["wechat_webhook"], "")

    def test_corrupt_ledger_is_reported_unavailable_without_secret(self):
        login_admin(self.client)
        self.ledger.write_text("{broken", encoding="utf-8")

        response = self.client.get("/api/reflow/alert-settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["last_delivery_status"], "unavailable")
        self.assertNotIn("webhook/send?key=", response.get_data(as_text=True))

    def test_test_route_uses_saved_webhook_without_returning_it(self):
        login_admin(self.client)
        with patch.object(
            admin_server,
            "test_wechat_webhook",
            return_value={"last_test_ok": True, "webhook_mask": "****1234"},
        ) as send:
            response = self.client.post("/api/reflow/alert-settings/test")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["last_test_ok"])
        self.assertNotIn("wechat_webhook", response.get_json())
        send.assert_called_once()

    def test_alert_controls_keep_mask_on_blank_save_and_call_test_route(self):
        run_admin_javascript(
            textwrap.dedent(
                """
                  renderReflow(content);
                  requests[0].resolve(response(true, {
                    auto_scan_enabled:true, updated_at:1, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:2, last_auto_error:'',
                    scheduler_status:'available'
                  }));
                  requests[1].resolve(response(true, {
                    wechat_enabled:false, webhook_configured:true,
                    webhook_mask:'****1234', last_test_at:0,
                    last_test_ok:false, last_test_error:'',
                    last_delivery_at:0, last_delivery_status:'none',
                    last_delivery_alert_id:0, last_delivery_error:''
                  }));
                  await flush();
                  assert.strictEqual(elements.reflowWebhookMask.textContent, '****1234');
                  elements.reflowWechatEnabled.checked=true;
                  elements.reflowWebhook.value='';
                  saveReflowAlertSetting();
                  assert.strictEqual(
                    JSON.parse(requests[2].args[1].body).wechat_webhook, ''
                  );
                  requests[2].resolve(response(true, {
                    ok:true, wechat_enabled:true, webhook_configured:true,
                    webhook_mask:'****1234'
                  }));
                  await flush();
                  assert.strictEqual(elements.reflowWebhookMask.textContent, '****1234');
                  testReflowWechat();
                  assert.strictEqual(
                    requests[3].args[0], '/api/reflow/alert-settings/test'
                  );
                  requests[3].resolve(response(true, {
                    last_test_ok:true, webhook_mask:'****1234'
                  }));
                  await flush();
                  assert.strictEqual(
                    elements.reflowWechatTestState.textContent, '测试消息已发送'
                  );
                """
            )
        )


if __name__ == "__main__":
    unittest.main()
