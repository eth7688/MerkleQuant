import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from momentum_reflow_alerts import (
    baseline_wechat_delivery,
    deliver_due_wechat,
    format_wechat_markdown,
    load_alert_settings,
    observe_reflow_alerts,
    public_alert_settings,
    read_delivery_status,
    read_public_alerts,
    save_alert_settings,
    send_wechat_markdown,
    test_wechat_webhook,
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

    def test_semantically_inconsistent_ledger_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            path.write_text(json.dumps({
                "version": 1, "initialized": True, "next_alert_id": 2,
                "wechat_cursor": 0, "observed": {}, "events": [{
                    "alert_id": 1, "signal_key": "BTC-LONG-1", "created_at": 1,
                    "trigger": "new_signal", "snapshot": {}, "wechat": {
                        "status": "pending", "attempts": 0, "last_attempt_at": 0,
                        "next_attempt_at": 1, "last_error": "",
                    },
                }],
            }), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                observe_reflow_alerts(path, [row()], 2_000)
            self.assertEqual(path.read_bytes(), original)

    def test_uninitialized_ledger_with_observed_state_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            path.write_text(json.dumps({
                "version": 1, "initialized": False, "next_alert_id": 1,
                "wechat_cursor": 0,
                "observed": {"BTC-LONG-1": {
                    "ever_high": True, "last_quality_label": "HIGH",
                }},
                "events": [],
            }), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                observe_reflow_alerts(path, [row()], 2_000)
            self.assertEqual(path.read_bytes(), original)

    def test_snapshot_with_extra_webhook_key_fails_closed_without_secret_response(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            secret = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            snapshot = {
                "symbol": "BTCUSDT", "direction": "LONG", "price": 100.0,
                "ema50": 99.0, "daily_kind": "strong_momentum", "window_index": 1,
                "breakout_volume_ratio": 3.0, "first_seen_at": 100,
                "wechat_webhook": secret,
            }
            path.write_text(json.dumps({
                "version": 1, "initialized": True, "next_alert_id": 2,
                "wechat_cursor": 0,
                "observed": {"BTC-LONG-1": {
                    "ever_high": True, "last_quality_label": "HIGH",
                }},
                "events": [{
                    "alert_id": 1, "signal_key": "BTC-LONG-1", "created_at": 1,
                    "trigger": "new_signal", "snapshot": snapshot, "wechat": {
                        "status": "pending", "attempts": 0, "last_attempt_at": 0,
                        "next_attempt_at": 1, "last_error": "",
                    },
                }],
            }), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                read_public_alerts(path, 0)
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_snapshot_field_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [], 1)
            observe_reflow_alerts(path, [row()], 2)
            ledger = json.loads(path.read_text(encoding="utf-8"))
            ledger["events"][0]["snapshot"]["first_seen_at"] = "bad"
            path.write_text(json.dumps(ledger), encoding="utf-8")
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


class ReflowWechatDeliveryTests(unittest.TestCase):
    def test_test_webhook_rejects_invalid_stored_webhook_without_overwrite(self):
        with TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            settings.write_text(json.dumps({
                "version": 1, "wechat_enabled": False,
                "wechat_webhook": "https://example.com/not-official",
                "updated_at": 1, "updated_by": "7", "last_test_at": 0,
                "last_test_ok": False, "last_test_error": "",
            }), encoding="utf-8")
            original = settings.read_bytes()
            with self.assertRaises(ValueError):
                test_wechat_webhook(settings, 2)
            self.assertEqual(settings.read_bytes(), original)

    def test_delivery_rejects_enabled_empty_stored_webhook_without_writing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            settings.write_text(json.dumps({
                "version": 1, "wechat_enabled": True, "wechat_webhook": "",
                "updated_at": 1, "updated_by": "7", "last_test_at": 0,
                "last_test_ok": False, "last_test_error": "",
            }), encoding="utf-8")
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            settings_before = settings.read_bytes(); ledger_before = ledger.read_bytes()
            with self.assertRaises(ValueError):
                deliver_due_wechat(settings, ledger, 2)
            self.assertEqual(settings.read_bytes(), settings_before)
            self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_concurrent_delivery_sends_once_and_advances_cursor_monotonically(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            first_started = threading.Event(); second_started = threading.Event(); release_first = threading.Event()
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            calls = []

            def post(*args, **kwargs):
                calls.append((args, kwargs))
                if len(calls) == 1:
                    first_started.set()
                    release_first.wait(1)
                else:
                    second_started.set()
                return response

            first = threading.Thread(target=deliver_due_wechat, args=(settings, ledger, 2), kwargs={"post": post})
            second = threading.Thread(target=deliver_due_wechat, args=(settings, ledger, 2), kwargs={"post": post})
            first.start(); self.assertTrue(first_started.wait(1))
            second.start()
            second_entered = second_started.wait(0.2)
            release_first.set(); first.join(1); second.join(1)
            self.assertFalse(second_entered)
            self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))["wechat_cursor"], 1)

    def test_delivery_rejects_non_integer_timestamp_before_writing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            original = ledger.read_bytes()
            with self.assertRaises(TypeError):
                deliver_due_wechat(settings, ledger, True)
            self.assertEqual(ledger.read_bytes(), original)

    def test_markdown_uses_frozen_snapshot(self):
        event = {"trigger": "upgraded_high", "snapshot": row(
            price=101.25, direction="SHORT", daily_kind="bearish_engulfing",
            first_seen_at=1_786_118_400_000,
        )}
        text = format_wechat_markdown(event)
        self.assertIn("BTCUSDT · SHORT", text)
        self.assertIn("价格：101.25", text)
        self.assertIn("日线：看跌吞没", text)
        self.assertIn("触发：标准信号升级为高质量", text)
        self.assertIn("首次发现：2026-08-08", text)
        self.assertNotIn("建议", text)

    def test_sender_requires_official_https_webhook_and_success_code(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        post = Mock(return_value=response)
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
        send_wechat_markdown(url, "test", post=post)
        post.assert_called_once_with(url, json={"msgtype": "markdown", "markdown": {"content": "test"}}, timeout=5.0)
        with self.assertRaises(ValueError):
            send_wechat_markdown("http://example.com/key=secret", "test", post=post)

    def test_business_failure_is_sanitized_and_scheduled_for_retry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1)
            observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None
            response.json.return_value = {"errcode": 93000, "errmsg": f"bad {url}"}
            result = deliver_due_wechat(settings, ledger, 2, post=Mock(return_value=response))
            raw = ledger.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "retry_pending")
            self.assertNotIn(url, raw)
            self.assertNotIn("secret-1234", raw)

    def test_timeout_retries_then_successfully_delivers_same_event(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            post = Mock(side_effect=[TimeoutError("timeout"), response])
            self.assertEqual(deliver_due_wechat(settings, ledger, 2, post=post)["status"], "retry_pending")
            self.assertEqual(deliver_due_wechat(settings, ledger, 60_002, post=post)["status"], "delivered")
            self.assertEqual(post.call_count, 2)

    def test_fifth_failure_marks_event_failed_and_advances_cursor(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 93000}
            now = 2
            for _ in range(5):
                result = deliver_due_wechat(settings, ledger, now, post=Mock(return_value=response))
                now = json.loads(ledger.read_text(encoding="utf-8"))["events"][0]["wechat"]["next_attempt_at"]
            self.assertEqual(result["status"], "failed")
            self.assertEqual(read_delivery_status(ledger)["last_delivery_status"], "failed")

    def test_success_advances_cursor_and_does_not_resend(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            post = Mock(return_value=response)
            self.assertEqual(deliver_due_wechat(settings, ledger, 2, post=post)["status"], "delivered")
            self.assertEqual(deliver_due_wechat(settings, ledger, 3, post=post)["status"], "idle")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(read_delivery_status(ledger)["last_delivery_status"], "delivered")

    def test_retry_waiting_event_blocks_later_event(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1)
            observe_reflow_alerts(ledger, [row("first")], 2)
            observe_reflow_alerts(ledger, [row("second")], 3)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 93000}
            post = Mock(return_value=response)
            deliver_due_wechat(settings, ledger, 3, post=post)
            result = deliver_due_wechat(settings, ledger, 4, post=post)
            self.assertEqual(result["status"], "waiting_retry")
            self.assertEqual(post.call_count, 1)

    def test_test_message_does_not_create_event_or_advance_cursor(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=False, wechat_webhook=url, updated_by="7", now_ms=1)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            result = test_wechat_webhook(settings, 2, post=Mock(return_value=response))
            self.assertTrue(result["last_test_ok"])
            self.assertFalse(ledger.exists())
