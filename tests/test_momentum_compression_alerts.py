import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from momentum_compression_alerts import (
    append_compression_alerts,
    baseline_compression_alerts,
    deliver_due_compression_wechat,
    read_public_compression_alerts,
)
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

    def test_same_compression_id_is_idempotent(self):
        with TemporaryDirectory() as folder:
            events, state, _ = self.paths(Path(folder))
            item = fresh(compression_id="same")
            self.assertEqual(len(append_compression_alerts(events, state, [item], 10)), 1)
            self.assertEqual(append_compression_alerts(events, state, [item], 11), [])
            self.assertEqual(read_public_compression_alerts(events, 0)["latest_alert_id"], 1)

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

    def test_webhook_error_is_sanitized_and_retried(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=webhook, updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            failed = Mock(); failed.raise_for_status.side_effect = RuntimeError(f"failed {webhook}")
            result = deliver_due_compression_wechat(settings, state, events, 10, post=Mock(return_value=failed))
            raw = state.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "retry_pending")
            self.assertNotIn(webhook, raw); self.assertNotIn("secret-1234", raw)
            self.assertEqual(json.loads(raw)["delivery_queue"][0]["next_attempt_at"], 10 + RETRY_DELAYS_MS[0])

    def test_fifth_failure_marks_terminal_failed(self):
        with TemporaryDirectory() as folder:
            events, state, settings = self.paths(Path(folder))
            save_alert_settings(settings, wechat_enabled=True,
                wechat_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret", updated_by="7", now_ms=1)
            append_compression_alerts(events, state, [fresh()], 10)
            failure = Mock(); failure.raise_for_status.side_effect = TimeoutError("timeout")
            now = 10
            for _ in range(5):
                result = deliver_due_compression_wechat(settings, state, events, now, post=Mock(return_value=failure))
                now = json.loads(state.read_text(encoding="utf-8"))["delivery_queue"][0]["next_attempt_at"]
            self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
