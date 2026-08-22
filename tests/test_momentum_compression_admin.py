import unittest
from unittest.mock import patch

with patch("trader.SqueezeBreakoutBot.start"):
    import web_ui


class CompressionInternalApiTests(unittest.TestCase):
    def setUp(self):
        web_ui.app.config.update(TESTING=True)
        self.client = web_ui.app.test_client()

    def test_internal_endpoints_reject_non_loopback_requests(self):
        remote = {"REMOTE_ADDR": "203.0.113.10"}

        self.assertEqual(
            self.client.get("/internal/compression/status", environ_overrides=remote).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/internal/compression/automation",
                json={"enabled": True},
                environ_overrides=remote,
            ).status_code,
            403,
        )

    def test_loopback_status_returns_only_monitor_and_scan_facts(self):
        monitor = {
            "running": True,
            "auto_enabled": True,
            "last_scan_at": 101,
            "next_scan_at": "2026-08-22T00:15:00+00:00",
            "structure_scanning": False,
            "scan_started_at": 99,
            "scan_duration_ms": 456,
            "scan_overdue": True,
            "last_error": "",
            "unexpected": "must not leak",
        }
        with patch.object(web_ui._compression_monitor, "status", return_value=monitor), patch.object(
            web_ui, "_compression_scan_summary", {"scanned": 12, "eligible": 3, "errors": 1}
        ):
            response = self.client.get("/internal/compression/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "monitor": {
                    "running": True,
                    "auto_enabled": True,
                    "last_scan_at": 101,
                    "next_scan_at": "2026-08-22T00:15:00+00:00",
                    "structure_scanning": False,
                    "scan_started_at": 99,
                    "scan_duration_ms": 456,
                    "scan_overdue": True,
                    "last_error": "",
                },
                "scan": {"scanned": 12, "eligible": 3, "errors": 1},
            },
        )

    def test_loopback_boolean_enable_uses_monitor_switch(self):
        with patch.object(
            web_ui._compression_monitor,
            "set_auto_enabled",
            return_value={"auto_enabled": True},
        ) as set_enabled:
            response = self.client.post(
                "/internal/compression/automation", json={"enabled": True}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"auto_enabled": True})
        set_enabled.assert_called_once_with(True)

    def test_loopback_automation_rejects_extra_payload_fields(self):
        with patch.object(
            web_ui._compression_monitor,
            "set_auto_enabled",
            return_value={"auto_enabled": True},
        ) as set_enabled:
            response = self.client.post(
                "/internal/compression/automation",
                json={"enabled": True, "extra": "x"},
            )

        self.assertEqual(response.status_code, 400)
        set_enabled.assert_not_called()


if __name__ == "__main__":
    unittest.main()
