import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from momentum_reflow_alerts import (
    baseline_wechat_delivery,
    load_alert_settings,
    observe_reflow_alerts,
    public_alert_settings,
    read_delivery_status,
    read_public_alerts,
    save_alert_settings,
)


def row(key="BTC-LONG-1", quality="HIGH", status="ACTIVE", **overrides):
    value = {
        "signal_key": key, "symbol": "BTCUSDT", "direction": "LONG",
        "quality_label": quality, "status": status, "price": 100.0,
        "ema50": 99.0, "daily_kind": "strong_momentum", "window_index": 1,
        "breakout_volume_ratio": 3.0, "first_seen_at": 100,
    }
    value.update(overrides)
    return value


class ReflowAlertLedgerTests(unittest.TestCase):
    def test_first_observation_baselines_existing_high_without_event(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            self.assertEqual(observe_reflow_alerts(path, [row()], 1_000), [])
            self.assertEqual(read_public_alerts(path, 0), {"latest_alert_id": 0, "events": []})

    def test_standard_upgrade_emits_once_and_snapshot_is_immutable(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [row(quality="STANDARD")], 1_000)
            created = observe_reflow_alerts(path, [row(price=101.0)], 2_000)
            duplicate = observe_reflow_alerts(path, [row(price=102.0)], 3_000)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["alert_id"], 1)
            self.assertEqual(created[0]["trigger"], "upgraded_high")
            self.assertEqual(created[0]["snapshot"]["price"], 101.0)
            self.assertEqual(duplicate, [])
            self.assertEqual(read_public_alerts(path, 0)["events"][0]["price"], 101.0)

    def test_only_high_active_rows_emit(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [], 1_000)
            created = observe_reflow_alerts(path, [
                row("watch", "WATCH"), row("standard", "STANDARD"),
                row("invalid", "HIGH", "INVALID"), row("active", "HIGH", "ACTIVE"),
            ], 2_000)
            self.assertEqual([event["signal_key"] for event in created], ["active"])
            self.assertEqual(created[0]["trigger"], "new_signal")

    def test_corrupt_ledger_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            path.write_text("{broken", encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                observe_reflow_alerts(path, [row()], 1_000)
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_nested_event_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            path.write_text(json.dumps({
                "version": 1, "initialized": True, "next_alert_id": 2,
                "wechat_cursor": 0, "observed": {}, "events": [{"alert_id": "bad"}],
            }), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                read_public_alerts(path, 0)
            self.assertEqual(path.read_bytes(), original)

    def test_webhook_is_masked_and_blank_save_preserves_secret(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            save_alert_settings(path, wechat_enabled=False, wechat_webhook=url,
                                updated_by="7", now_ms=100)
            saved = save_alert_settings(path, wechat_enabled=True, wechat_webhook="",
                                        updated_by="7", now_ms=200)
            public = public_alert_settings(saved)
            self.assertEqual(saved["wechat_webhook"], url)
            self.assertNotIn(url, json.dumps(public))
            self.assertTrue(public["webhook_configured"])
            self.assertEqual(public["webhook_mask"], "****1234")

    def test_non_official_webhook_is_rejected_before_persistence(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with self.assertRaises(ValueError):
                save_alert_settings(path, wechat_enabled=False,
                    wechat_webhook="https://example.com/hook", updated_by="7", now_ms=100)

    def test_enabling_channel_baselines_latest_event(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [], 1_000)
            observe_reflow_alerts(path, [row()], 2_000)
            self.assertEqual(baseline_wechat_delivery(path), 1)
