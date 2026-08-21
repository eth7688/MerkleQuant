import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

from momentum_compression_alerts import (
    append_compression_alerts,
    baseline_compression_alerts,
    compression_delivery_statuses,
    compression_sound_available_ids,
    deliver_due_compression_wechat,
    drain_compression_outbox,
    format_compression_wechat_markdown,
    read_public_compression_alerts,
)
from momentum_compression_store import apply_live_prices, default_state, reconcile_structure_scan, save_compression_state
from momentum_reflow_alerts import RETRY_DELAYS_MS, save_alert_settings


def fresh(symbol="AUSDT", alignment="CONFIRMED", compression_id=None):
    return {
        "event_id": 1,
        "compression_id": compression_id or f"{symbol}-long-1",
        "state": "BREAKOUT_FRESH_LONG",
        "event_at": 10_000,
        "live_price": 101.25,
        "htf_alignment": alignment,
        "structure": {
            "symbol": symbol, "side": "LONG", "upper_boundary_price": 100.0,
            "lower_boundary_price": 90.0, "breakout_buffer_price": 0.5,
            "compression_bars": 20, "parameter_version": "15m-compression-v1",
        },
    }


class CompressionAlertTests(unittest.TestCase):
    def paths(self, root):
        return root / "events.jsonl", root / "state.json", root / "settings.json"

    def queued_symbols(self, state_path):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return [item["event"]["structure"]["symbol"] for item in state["delivery_queue"]]

    def public_symbols(self, events_path):
        return [item["structure"]["symbol"] for item in read_public_compression_alerts(events_path, 0)["events"]]

    def test_all_fresh_events_are_public_but_only_confirmed_is_queued(self):
        with TemporaryDirectory() as folder:
            events, state, _ = self.paths(Path(folder))
            created = append_compression_alerts(events, state, [
                fresh("AUSDT", "CONFIRMED"), fresh("BUSDT", "CONFLICT"), fresh("CUSDT", "UNKNOWN"),
            ], 10_000)
            self.assertEqual(len(created), 3)
            self.assertEqual(self.public_symbols(events), ["AUSDT", "BUSDT", "CUSDT"])
            self.assertEqual(self.queued_symbols(state), ["AUSDT"])
            self.assertEqual(compression_sound_available_ids(events), {"AUSDT-long-1", "BUSDT-long-1", "CUSDT-long-1"})

    def test_delivery_statuses_are_keyed_by_confirmed_compression_identity(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            created = append_compression_alerts(events, state, [fresh("AUSDT")], 10_000)
            self.assertEqual(compression_delivery_statuses(state), {created[0]["compression_id"]: "pending"})
            save_alert_settings(settings, wechat_enabled=True,
                wechat_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", updated_by="7", now_ms=1)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            deliver_due_compression_wechat(settings, state, events, 10_000, post=Mock(return_value=response))
            self.assertEqual(compression_delivery_statuses(state), {created[0]["compression_id"]: "delivered"})

    def test_same_compression_id_is_idempotent(self):
        with TemporaryDirectory() as folder:
            events, state, _ = self.paths(Path(folder))
            item = fresh(compression_id="same")
            self.assertEqual(len(append_compression_alerts(events, state, [item], 10)), 1)
            self.assertEqual(append_compression_alerts(events, state, [item], 11), [])
            self.assertEqual(read_public_compression_alerts(events, 0)["latest_alert_id"], 1)

    def test_durable_outbox_recovers_public_and_confirmed_delivery_after_callback_failure(self):
        structure = {
            "symbol": "AUSDT", "side": "LONG", "compression_id": "outbox-1", "state": "PRE_BREAKOUT",
            "fresh_emitted": False, "upper_boundary_price": 100.0, "lower_boundary_price": 90.0,
            "breakout_buffer_price": 0.5, "first_seen_at": 1, "last_verified_at": 1, "htf_alignment": "CONFIRMED",
        }
        with TemporaryDirectory() as folder:
            root = Path(folder); compression_state = root / "compression.json"; events, alert_state, _ = self.paths(root)
            state, _ = reconcile_structure_scan(default_state(), [structure], 1)
            state, fresh_events = apply_live_prices(state, {"AUSDT": 101.0}, 2)
            save_compression_state(compression_state, state)
            with patch("momentum_compression_alerts._atomic_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    drain_compression_outbox(compression_state, events, alert_state, 3)
            self.assertEqual(drain_compression_outbox(compression_state, events, alert_state, 4), [])
            self.assertEqual(read_public_compression_alerts(events, 0)["latest_alert_id"], 1)
            self.assertEqual(self.queued_symbols(alert_state), ["AUSDT"])
            self.assertEqual(len(fresh_events), 1)

    def test_confirmed_markdown_has_required_compression_audit_fields(self):
        event = fresh()
        event["structure"].update({
            "atr14": 0.4, "quality_score": 88.0, "compression_bars": 21,
            "directional_touch_count": 3, "contraction_ratio": 0.5,
            "htf_timeframes": {"1h": True, "4h": True},
            "ohlcv_snapshot_ref": "momentum_compression_snapshots/AUSDT.json",
        })
        event = {**event, "alert_id": 1, "created_at": 10}
        text = format_compression_wechat_markdown(event)
        for field in ("触发价格", "突破边界/缓冲", "上沿", "下沿", "ATR14", "质量", "K线", "方向触碰", "收敛", "1H", "4H", "快照", "北京时间"):
            self.assertIn(field, text)

    def test_retry_recovers_confirmed_queue_after_state_write_fails_post_append(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            item = fresh(compression_id="recover-me")
            with patch("momentum_compression_alerts._atomic_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    append_compression_alerts(events, state, [item], 10)

            self.assertEqual(read_public_compression_alerts(events, 0)["latest_alert_id"], 1)
            self.assertEqual(append_compression_alerts(events, state, [item], 11), [])
            self.assertEqual(self.queued_symbols(state), ["AUSDT"])

            save_alert_settings(settings, wechat_enabled=True,
                wechat_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", updated_by="7", now_ms=1)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            self.assertEqual(
                deliver_due_compression_wechat(settings, state, events, 11, post=Mock(return_value=response))["status"],
                "delivered",
            )
            self.assertEqual(append_compression_alerts(events, state, [item], 12), [])
            self.assertEqual(self.public_symbols(events), ["AUSDT"])
            self.assertEqual(self.queued_symbols(state), ["AUSDT"])

    def test_baseline_uses_latest_event_without_replaying_history(self):
        with TemporaryDirectory() as folder:
            events, state, _ = self.paths(Path(folder))
            append_compression_alerts(events, state, [fresh("OLDUSDT")], 10)
            self.assertEqual(baseline_compression_alerts(events), 1)
            self.assertEqual(read_public_compression_alerts(events, 1)["events"], [])

    def test_delivery_serializes_concurrent_callers_to_one_http_send(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=webhook, updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            started, release, calls, results = threading.Event(), threading.Event(), [], []
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}

            def post(*args, **kwargs):
                calls.append((args, kwargs)); started.set(); release.wait(1); return response

            first = threading.Thread(target=lambda: results.append(deliver_due_compression_wechat(settings, state, events, 10, post=post)))
            second = threading.Thread(target=lambda: results.append(deliver_due_compression_wechat(settings, state, events, 10, post=post)))
            first.start(); self.assertTrue(started.wait(1)); second.start(); release.set(); first.join(2); second.join(2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(item["status"] for item in results), ["delivered", "idle"])

    def test_post_send_state_write_failure_becomes_indeterminate_and_never_resends(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            save_alert_settings(settings, wechat_enabled=True,
                wechat_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            sent = Mock(return_value=response)
            from momentum_compression_alerts import _atomic_write as real_atomic_write
            writes = 0

            def fail_final_write(path, payload):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("post-send disk full")
                real_atomic_write(path, payload)

            with patch("momentum_compression_alerts._atomic_write", side_effect=fail_final_write):
                with self.assertRaisesRegex(OSError, "post-send"):
                    deliver_due_compression_wechat(settings, state, events, 10, post=sent)

            self.assertEqual(sent.call_count, 1)
            restarted = deliver_due_compression_wechat(settings, state, events, 20, post=sent)
            self.assertEqual(restarted["status"], "idle")
            self.assertEqual(sent.call_count, 1)
            self.assertEqual(compression_delivery_statuses(state), {"AUSDT-long-1": "indeterminate"})

    def test_read_timeout_after_recorded_request_is_indeterminate_and_never_resent(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            save_alert_settings(settings, wechat_enabled=True,
                wechat_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            calls = []

            def post(*args, **kwargs):
                calls.append((args, kwargs))
                raise requests.ReadTimeout("response timed out after request was sent")

            first = deliver_due_compression_wechat(settings, state, events, 10, post=post)
            restarted = deliver_due_compression_wechat(settings, state, events, 1_000_000, post=post)

            self.assertEqual(first["status"], "indeterminate")
            self.assertEqual(restarted["status"], "idle")
            self.assertEqual(len(calls), 1)
            self.assertEqual(compression_delivery_statuses(state), {"AUSDT-long-1": "indeterminate"})

    def test_rejected_wecom_response_is_sanitized_and_terminal_failed(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=webhook, updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            failed = Mock(); failed.raise_for_status.return_value = None; failed.json.return_value = {"errcode": 93000}
            result = deliver_due_compression_wechat(settings, state, events, 10, post=Mock(return_value=failed))
            raw = state.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "failed")
            self.assertNotIn(webhook, raw); self.assertNotIn("secret-1234", raw)
            self.assertEqual(json.loads(raw)["delivery_queue"][0]["next_attempt_at"], 0)

    def test_explicit_presend_validation_failure_is_retryable_without_http_call(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            save_alert_settings(settings, wechat_enabled=True,
                wechat_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            post = Mock()
            with patch("momentum_compression_alerts.validate_wechat_webhook", side_effect=ValueError("invalid webhook")):
                result = deliver_due_compression_wechat(settings, state, events, 10, post=post)
            self.assertEqual(result["status"], "retry_pending")
            self.assertEqual(post.call_count, 0)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["delivery_queue"][0]["next_attempt_at"], 10 + RETRY_DELAYS_MS[0])


if __name__ == "__main__":
    unittest.main()
