import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import admin_server
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

        const ids = [
          'reflowAutoEnabled', 'reflowEnabledState', 'reflowUpdatedAt',
          'reflowUpdatedBy', 'reflowLastScan', 'reflowNextScan',
          'reflowLastError', 'reflowSaveError'
        ];
        let elements = {};
        function newElement(id) {
          return {
            id:id, checked:false, disabled:false, dataset:{},
            style:{display:'none'}, textContent:'', onchange:null
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
        self.assertIn("200万 USDT", html)
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

                  requests[1].resolve(response(true, {
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
                  requests[1].resolve(response(false, {error:'stale failure'}));
                  await flush();

                  assert.strictEqual(detachedBox.checked, true);
                  assert.strictEqual(detachedBox.disabled, true);
                  assert.strictEqual(detachedError.style.display, 'none');
                  assert.strictEqual(detachedError.textContent, '');
                  assert.strictEqual(currentBox.disabled, true);
                  assert.strictEqual(currentError.style.display, 'none');

                  requests[2].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:300, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:400,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  currentBox.checked = true;
                  currentBox.onchange();
                  requests[3].resolve(response(false, {error:'current failure'}));
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
                  requests[2].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  assert.strictEqual(oldSnapshotBox.checked, false);

                  requests[1].resolve(response(true, {
                    ok:true, auto_scan_enabled:true,
                    updated_at:300, updated_by:'admin'
                  }));
                  await flush();

                  assert.strictEqual(requests.length, 4);
                  assert.strictEqual(detachedBox.checked, true);
                  assert.strictEqual(detachedBox.disabled, true);
                  assert.strictEqual(detachedBox.onchange instanceof Function, true);
                  const refreshedBox = elements.reflowAutoEnabled;
                  const refreshedState = elements.reflowEnabledState;
                  assert.notStrictEqual(refreshedBox, oldSnapshotBox);
                  requests[3].resolve(response(true, {
                    auto_scan_enabled:true, updated_at:300, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:400,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();

                  assert.strictEqual(refreshedBox.checked, true);
                  assert.strictEqual(refreshedBox.disabled, false);
                  assert.strictEqual(refreshedState.textContent, '已启用');
                  await flush();
                  assert.strictEqual(requests.length, 4);
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
                  requests[2].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:100, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:200,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  const secondBox = elements.reflowAutoEnabled;
                  const secondError = elements.reflowSaveError;
                  secondBox.checked = false;
                  secondBox.onchange();

                  requests[1].resolve(response(true, {
                    ok:true, auto_scan_enabled:true,
                    updated_at:300, updated_by:'admin'
                  }));
                  await flush();
                  assert.strictEqual(requests.length, 5);
                  const interimBox = elements.reflowAutoEnabled;
                  requests[4].resolve(response(true, {
                    auto_scan_enabled:true, updated_at:300, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:400,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();
                  assert.strictEqual(interimBox.checked, true);

                  requests[3].resolve(response(true, {
                    ok:true, auto_scan_enabled:false,
                    updated_at:500, updated_by:'admin'
                  }));
                  await flush();
                  assert.strictEqual(requests.length, 6);
                  assert.strictEqual(secondBox.checked, false);
                  assert.strictEqual(secondBox.disabled, true);
                  assert.strictEqual(secondError.style.display, 'none');
                  const finalBox = elements.reflowAutoEnabled;
                  const finalState = elements.reflowEnabledState;
                  requests[5].resolve(response(true, {
                    auto_scan_enabled:false, updated_at:500, updated_by:'admin',
                    last_auto_scan_at:0, next_scan_at:600,
                    last_auto_error:'', scheduler_status:'available'
                  }));
                  await flush();

                  assert.strictEqual(finalBox.checked, false);
                  assert.strictEqual(finalBox.disabled, false);
                  assert.strictEqual(finalState.textContent, '已关闭');
                  await flush();
                  assert.strictEqual(requests.length, 6);
                """
            )
        )


if __name__ == "__main__":
    unittest.main()
