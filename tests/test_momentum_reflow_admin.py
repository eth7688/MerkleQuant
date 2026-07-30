import tempfile
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
            "scheduler_status": "running",
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
        self.assertEqual(payload["scheduler_status"], "running")
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


if __name__ == "__main__":
    unittest.main()
