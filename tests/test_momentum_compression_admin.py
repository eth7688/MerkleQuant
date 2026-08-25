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
            "price_stream_status": "stale",
            "last_price_message_at": 88,
            "last_error": "",
            "rejection_counts": {"INSUFFICIENT_PIVOTS": 7},
            "tier_counts": {"STRICT": 2, "WATCH": 1},
            "strict_rejection_counts": {"INSUFFICIENT_PIVOTS": 7},
            "watch_rejection_counts": {"DIRECTIONAL_TOUCHES": 3},
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
                    "price_stream_status": "stale",
                    "last_price_message_at": 88,
                    "last_error": "",
                },
                "scan": {
                    "scanned": 12,
                    "eligible": 3,
                    "errors": 1,
                    "rejection_counts": {"INSUFFICIENT_PIVOTS": 7},
                    "tier_counts": {"STRICT": 2, "WATCH": 1},
                    "strict_rejection_counts": {"INSUFFICIENT_PIVOTS": 7},
                    "watch_rejection_counts": {"DIRECTIONAL_TOUCHES": 3},
                },
            },
        )

    def test_loopback_status_excludes_pool_alert_and_webhook_facts(self):
        monitor = {
            "tier_counts": {"STRICT": 1, "WATCH": 2},
            "strict_rejection_counts": {"A": 3},
            "watch_rejection_counts": {"B": 4},
        }
        with patch.object(web_ui._compression_monitor, "status", return_value=monitor):
            response = self.client.get("/internal/compression/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["scan"]["tier_counts"], {"STRICT": 1, "WATCH": 2})
        self.assertEqual(payload["scan"]["strict_rejection_counts"], {"A": 3})
        self.assertEqual(payload["scan"]["watch_rejection_counts"], {"B": 4})
        serialized = str(payload)
        for forbidden in ("pool_rows", "symbol", "alert", "delivery_queue", "webhook"):
            self.assertNotIn(forbidden, serialized)

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
