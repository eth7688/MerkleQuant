import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from momentum_compression_store import default_state, save_compression_state


def pool_item(symbol="POOLUSDT"):
    return {
        "symbol": symbol, "side": "LONG", "compression_id": "pool-long",
        "state": "PRE_BREAKOUT", "fresh_emitted": False,
        "upper_boundary_price": 11.0, "lower_boundary_price": 9.0,
        "breakout_buffer_price": 0.5, "first_seen_at": 1,
        "last_verified_at": 1,
    }


class SchedulerTests(unittest.TestCase):
    def test_next_closed_15m_scan_waits_five_seconds_after_close_in_utc(self):
        from momentum_compression_monitor import next_closed_15m_scan_at

        self.assertEqual(
            next_closed_15m_scan_at(datetime(2026, 8, 21, 12, 14, 59, tzinfo=timezone.utc)),
            datetime(2026, 8, 21, 12, 15, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_closed_15m_scan_at(datetime(2026, 8, 21, 12, 15, 5, tzinfo=timezone.utc)),
            datetime(2026, 8, 21, 12, 30, 5, tzinfo=timezone.utc),
        )

    def test_next_closed_15m_scan_normalizes_non_utc_aware_time(self):
        from momentum_compression_monitor import next_closed_15m_scan_at

        beijing = timezone(timedelta(hours=8))
        self.assertEqual(
            next_closed_15m_scan_at(datetime(2026, 8, 21, 20, 14, 59, tzinfo=beijing)),
            datetime(2026, 8, 21, 12, 15, 5, tzinfo=timezone.utc),
        )


class CompressionMonitorPriceTests(unittest.TestCase):
    def _monitor(self, root, **kwargs):
        from momentum_compression_monitor import CompressionMonitor

        state_path = root / "state.json"
        state = default_state()
        state["pool"]["pool-long"] = pool_item()
        save_compression_state(state_path, state)
        return CompressionMonitor(state_path, root, lambda events: None, **kwargs)

    def test_message_filters_to_pool_before_applying_prices(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            applied = {}
            monitor._apply_prices = lambda prices, now_ms: applied.update(prices)
            monitor.handle_message(json.dumps([
                {"s": "POOLUSDT", "c": "10.5"},
                {"s": "OTHERUSDT", "c": "99"},
            ]), now_ms=2_000)

        self.assertEqual(applied, {"POOLUSDT": 10.5})

    def test_rest_fallback_filters_pool_prices(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"symbol": "POOLUSDT", "price": "10.5"}, {"symbol": "OTHERUSDT", "price": "99"}]

        calls = []
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder), http_get=lambda *args, **kwargs: calls.append(args) or Response())
            applied = {}
            monitor._apply_prices = lambda prices, now_ms: applied.update(prices)
            monitor.rest_fallback_once(now_ms=2_000)

        self.assertEqual(len(calls), 1)
        self.assertEqual(applied, {"POOLUSDT": 10.5})

    def test_reconnect_delays_follow_documented_caps(self):
        from momentum_compression_monitor import CompressionMonitor

        self.assertEqual([CompressionMonitor.reconnect_delay(attempt) for attempt in range(7)], [1, 2, 5, 10, 30, 30, 30])

    def test_malformed_or_non_pool_rows_are_counted_without_stopping_updates(self):
        with TemporaryDirectory() as folder:
            monitor = self._monitor(Path(folder))
            monitor.handle_message(json.dumps([
                {"s": "POOLUSDT", "c": "bad"},
                {"s": "OTHERUSDT", "c": "99"},
                "bad-row",
            ]), now_ms=2_000)
            status = monitor.status()

        self.assertEqual(status["dropped_price_rows"], 3)

    def test_event_callback_can_read_status_after_state_is_saved(self):
        from momentum_compression_monitor import CompressionMonitor

        with TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / "state.json"
            state = default_state()
            state["pool"]["pool-long"] = {**pool_item(), "htf_alignment": "CONFIRMED"}
            save_compression_state(state_path, state)
            callbacks = []
            monitor = CompressionMonitor(state_path, root, lambda events: callbacks.append((events, monitor.status())))
            monitor.handle_message(json.dumps([{"s": "POOLUSDT", "c": "12"}]), now_ms=2_000)

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0][0]["state"], "BREAKOUT_FRESH_LONG")
        self.assertEqual(callbacks[0][1]["today_fresh"], 1)

    def test_disabling_auto_stops_monitoring_but_preserves_pool(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = self._monitor(root)
            status = monitor.set_auto_enabled(False)
            self.assertFalse(status["running"])
            self.assertEqual(status["pool_size"], 1)


if __name__ == "__main__":
    unittest.main()
