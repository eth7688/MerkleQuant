"""Real-time price monitoring and 15-minute scanning for compression pools."""

import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

from momentum_compression_service import scan_compression_market
from momentum_compression_store import apply_live_prices, compression_state_lock, load_compression_state, save_compression_state

try:
    import websocket
except ImportError:  # pragma: no cover - requirements supplies this in production
    websocket = None


FUTURES_MINI_TICKER_URL = "wss://fstream.binance.com/ws/!miniTicker@arr"
FUTURES_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
_RECONNECT_DELAYS = (1, 2, 5, 10, 30)
_REST_FALLBACK_TIMEOUT_SECONDS = 4


def next_closed_15m_scan_at(now: datetime) -> datetime:
    """Return the first 15m close plus its five second settlement allowance."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    candidate = now.replace(second=5, microsecond=0)
    minute = (now.minute // 15) * 15
    candidate = candidate.replace(minute=minute)
    if candidate <= now:
        candidate += timedelta(minutes=15)
    return candidate


class CompressionMonitor:
    """Runs no-trading price updates and structural scans against persisted state."""

    def __init__(
        self,
        state_path: Path,
        snapshot_dir: Path,
        event_callback,
        *,
        websocket_factory=None,
        http_get=requests.get,
        scan=scan_compression_market,
        now=datetime.now,
        time_ms=None,
        sleep=time.sleep,
        thread_factory=threading.Thread,
        stream_stale_after_ms=15_000,
    ):
        if (
            not isinstance(stream_stale_after_ms, int)
            or isinstance(stream_stale_after_ms, bool)
            or stream_stale_after_ms <= 0
        ):
            raise ValueError("stream_stale_after_ms must be a positive integer")
        self.state_path = Path(state_path)
        self.snapshot_dir = Path(snapshot_dir)
        self.event_callback = event_callback
        self.websocket_factory = websocket_factory or (websocket.WebSocketApp if websocket else None)
        self.http_get = http_get
        self.scan = scan
        self.now = now
        self.time_ms = time_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.thread_factory = thread_factory
        self.stream_stale_after_ms = stream_stale_after_ms
        self._state_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stream_commit_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []
        self._app = None
        self._close_intent_app = None
        self._running = False
        self._stream_connected = False
        self._stream_ever_connected = False
        self._price_stream_status = "stopped"
        self._last_stream_message_at_ms = 0
        self._last_scan_at = 0
        self._scan_started_at = 0
        self._scan_duration_ms = 0
        self._next_scan_at = None
        self._last_error = ""
        self._last_rejection_counts = {}
        self._dropped_price_rows = 0
        self._event_cursor = 0
        self._generation = 0
        self._active_worker_count = 0
        self._restart_pending = False

    @staticmethod
    def reconnect_delay(attempt: int) -> int:
        return _RECONNECT_DELAYS[min(max(0, attempt), len(_RECONNECT_DELAYS) - 1)]

    def _utc_now(self):
        try:
            value = self.now(timezone.utc)
        except TypeError:
            value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _pool_symbols(self):
        with self._state_lock:
            return {item["symbol"] for item in load_compression_state(self.state_path)["pool"].values()}

    def _commit_prices(self, prices: dict[str, float], now_ms: int) -> list[dict]:
        with self._state_lock:
            with compression_state_lock(self.state_path):
                state = load_compression_state(self.state_path)
                state, events = apply_live_prices(state, prices, now_ms)
                save_compression_state(self.state_path, state)
        return events

    def _apply_prices(self, prices: dict[str, float], now_ms: int, *, source_app=None) -> None:
        if not prices:
            return
        if source_app is None:
            events = self._commit_prices(prices, now_ms)
        else:
            events = None
            with self._stream_commit_lock:
                with self._lifecycle_lock:
                    if source_app is not self._app or source_app is self._close_intent_app:
                        return
                committed_events = self._commit_prices(prices, now_ms)
                with self._lifecycle_lock:
                    if source_app is self._app and source_app is not self._close_intent_app:
                        events = committed_events
        if events:
            try:
                self.event_callback(events)
            except Exception as error:
                self._last_error = f"event callback: {error}"

    @staticmethod
    def _finite_price(value):
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    def handle_message(self, message, *, now_ms=None, source_app=None) -> None:
        if source_app is not None:
            with self._lifecycle_lock:
                if source_app is not self._app or source_app is self._close_intent_app:
                    return
        try:
            rows = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            self._dropped_price_rows += 1
            self._last_error = "malformed mini ticker message"
            return
        if not isinstance(rows, list):
            self._dropped_price_rows += 1
            self._last_error = "malformed mini ticker message"
            return
        symbols = self._pool_symbols()
        prices = {}
        message_at = self.time_ms() if now_ms is None else now_ms
        has_valid_stream_price = False
        for row in rows:
            symbol = row.get("s") if isinstance(row, dict) else None
            price = self._finite_price(row.get("c")) if isinstance(row, dict) else None
            if isinstance(symbol, str) and symbol and price is not None:
                has_valid_stream_price = True
                if symbol in symbols:
                    prices[symbol] = price
                else:
                    self._dropped_price_rows += 1
            else:
                self._dropped_price_rows += 1
        if has_valid_stream_price:
            with self._lifecycle_lock:
                if (
                    source_app is not None
                    and (source_app is not self._app or source_app is self._close_intent_app)
                ):
                    return
                self._last_stream_message_at_ms = message_at
        if source_app is None:
            self._apply_prices(prices, message_at)
        else:
            self._apply_prices(prices, message_at, source_app=source_app)

    def rest_fallback_once(self, *, now_ms=None) -> bool:
        symbols = self._pool_symbols()
        if not symbols:
            return True
        try:
            response = self.http_get(FUTURES_PRICE_URL, timeout=_REST_FALLBACK_TIMEOUT_SECONDS)
            response.raise_for_status()
            rows = response.json()
        except Exception as error:
            self._last_error = f"REST fallback: {error}"
            return False
        prices = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("symbol") not in symbols:
                continue
            price = self._finite_price(row.get("price"))
            if price is not None:
                prices[row["symbol"]] = price
        self._apply_prices(prices, self.time_ms() if now_ms is None else now_ms)
        return True

    def _close_stale_stream(self, now_ms: int) -> bool:
        with self._lifecycle_lock:
            if (
                not self._stream_connected
                or self._last_stream_message_at_ms <= 0
                or now_ms - self._last_stream_message_at_ms < self.stream_stale_after_ms
            ):
                return False
            self._stream_connected = False
            self._price_stream_status = "stale"
            app = self._app
            self._close_intent_app = app
            self._app = None
        if app is not None:
            try:
                app.close()
            except Exception as error:
                self._last_error = f"WebSocket stale close: {error}"
        return True

    def _on_open(self, app):
        with self._lifecycle_lock:
            if app is not self._app or app is self._close_intent_app:
                return
            self._stream_connected = True
            self._stream_ever_connected = True
            self._price_stream_status = "connected"
            self._last_stream_message_at_ms = self.time_ms()

    def _on_message(self, app, message):
        self.handle_message(message, source_app=app)

    def _on_error(self, app, error):
        with self._lifecycle_lock:
            if app is not self._app or app is self._close_intent_app:
                return
            self._stream_connected = False
            self._last_error = f"WebSocket: {error}"

    def _on_close(self, app, *args):
        with self._lifecycle_lock:
            if app is not self._app or app is self._close_intent_app:
                return
            self._stream_connected = False
            if not self._stop.is_set():
                self._price_stream_status = "reconnecting"

    def _bind_stream_app(self, app) -> bool:
        with self._stream_commit_lock:
            with self._lifecycle_lock:
                if self._stop.is_set():
                    return False
                self._app = app
                self._close_intent_app = None
                return True

    def _stream_loop(self):
        attempt = 0
        while not self._stop.is_set():
            if self.websocket_factory is None:
                self._last_error = "websocket-client dependency is unavailable"
                with self._lifecycle_lock:
                    self._price_stream_status = "unavailable"
                return
            with self._lifecycle_lock:
                self._price_stream_status = "connecting"
                self._stream_ever_connected = False
            app = None
            try:
                app = self.websocket_factory(
                    FUTURES_MINI_TICKER_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                if not self._bind_stream_app(app):
                    break
                app.run_forever()
            except Exception as error:
                with self._lifecycle_lock:
                    if app is None or app is self._app:
                        self._stream_connected = False
                        self._last_error = f"WebSocket: {error}"
            with self._lifecycle_lock:
                if app is self._app:
                    self._app = None
                    self._stream_connected = False
                if self._stop.is_set():
                    break
                if self._price_stream_status != "stale":
                    self._price_stream_status = "reconnecting"
                stream_ever_connected = self._stream_ever_connected
            if stream_ever_connected:
                attempt = 0
            self.sleep(self.reconnect_delay(attempt))
            attempt += 1

    def _fallback_loop(self):
        while not self._stop.is_set():
            started_at_ms = self.time_ms()
            self.rest_fallback_once(now_ms=started_at_ms)
            delay_seconds = max(0, (started_at_ms + 5_000 - self.time_ms()) / 1_000)
            self.sleep(delay_seconds)

    def _watchdog_loop(self):
        while not self._stop.is_set():
            self._close_stale_stream(self.time_ms())
            self.sleep(1)

    def _scheduler_loop(self):
        while not self._stop.is_set():
            now = self._utc_now()
            if self._next_scan_at is None:
                self._next_scan_at = next_closed_15m_scan_at(now)
            if now >= self._next_scan_at:
                self.scan_now("scheduled")
                self._next_scan_at = next_closed_15m_scan_at(now)
            self.sleep(1)

    def scan_now(self, trigger: str) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            return False
        self._scan_started_at = self.time_ms()
        try:
            report = self.scan(self.state_path, self.snapshot_dir)
            raw_rejection_counts = report.get("rejection_counts", {})
            rejection_counts = {
                key: value
                for key, value in raw_rejection_counts.items()
                if isinstance(key, str) and type(value) is int and value >= 0
            } if isinstance(raw_rejection_counts, dict) else {}
            with self._lifecycle_lock:
                self._last_rejection_counts = rejection_counts
            events = [
                event for event in report.get("events", [])
                if isinstance(event, dict) and isinstance(event.get("event_id"), int)
                and event["event_id"] > self._event_cursor
            ]
            if events:
                self._event_cursor = max(event["event_id"] for event in events)
            self._last_scan_at = report.get("evaluated_at", self.time_ms())
            self._last_error = ""
            if events:
                try:
                    self.event_callback(events)
                except Exception as error:
                    self._last_error = f"event callback: {error}"
            return True
        except Exception as error:
            self._last_error = f"{trigger} scan: {error}"
            return False
        finally:
            self._scan_duration_ms = self.time_ms() - self._scan_started_at
            self._scan_lock.release()

    def start(self) -> bool:
        with self._state_lock:
            state = load_compression_state(self.state_path)
            if not state["auto_enabled"]:
                return False
            self._event_cursor = state["next_event_id"] - 1
        with self._lifecycle_lock:
            if self._running:
                if self._stop.is_set():
                    self._restart_pending = True
                return False
            self._stop.clear()
            self._running = True
            self._restart_pending = False
            self._generation += 1
            generation = self._generation
            workers = (self._stream_loop, self._fallback_loop, self._watchdog_loop, self._scheduler_loop)
            self._active_worker_count = len(workers)
            self._threads = [
                self.thread_factory(
                    target=lambda worker=worker: self._run_worker(worker, generation),
                    name=name,
                    daemon=True,
                )
                for worker, name in zip(workers, (
                    "compression-price-stream", "compression-price-fallback", "compression-price-watchdog",
                    "compression-15m-scheduler",
                ))
            ]
            for thread in self._threads:
                thread.start()
        return True

    def _run_worker(self, worker, generation):
        try:
            worker()
        finally:
            self._worker_exited(generation)

    def _worker_exited(self, generation):
        restart = False
        with self._lifecycle_lock:
            if generation != self._generation:
                return
            self._active_worker_count -= 1
            if self._active_worker_count:
                return
            self._running = False
            self._stream_connected = False
            self._price_stream_status = "stopped"
            restart = self._restart_pending
            self._restart_pending = False
        if restart:
            self.start()

    def stop(self, timeout: float = 2.0) -> bool:
        with self._lifecycle_lock:
            self._stop.set()
            app = self._app
            self._close_intent_app = app
            self._app = None
            self._stream_connected = False
            threads = tuple(self._threads)
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        deadline = time.monotonic() + timeout
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        with self._lifecycle_lock:
            still_running = self._running and self._active_worker_count > 0
            self._price_stream_status = "stopping" if still_running else "stopped"
        return not still_running

    def set_auto_enabled(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self._state_lock:
            with compression_state_lock(self.state_path):
                state = load_compression_state(self.state_path)
                state["auto_enabled"] = enabled
                save_compression_state(self.state_path, state)
                if enabled:
                    self._event_cursor = state["next_event_id"] - 1
        if enabled:
            self.start()
        else:
            with self._lifecycle_lock:
                self._restart_pending = False
            self.stop()
        return self.status()

    def status(self) -> dict:
        with self._state_lock:
            state = load_compression_state(self.state_path)
        with self._lifecycle_lock:
            running = self._running
            price_stream_status = self._price_stream_status
            last_price_message_at = self._last_stream_message_at_ms
            rejection_counts = dict(self._last_rejection_counts)
        structure_scanning = self._scan_lock.locked()
        return {
            "running": running,
            "auto_enabled": state["auto_enabled"],
            "structure_scanning": structure_scanning,
            "scan_started_at": self._scan_started_at,
            "scan_duration_ms": self._scan_duration_ms,
            "scan_overdue": structure_scanning and self._scan_started_at > 0
            and self.time_ms() - self._scan_started_at >= 900_000,
            "price_stream_status": price_stream_status,
            "last_price_message_at": last_price_message_at,
            "last_scan_at": self._last_scan_at or state["last_structure_scan_at"],
            "next_scan_at": self._next_scan_at.isoformat() if self._next_scan_at else None,
            "last_error": self._last_error or state["last_error"],
            "rejection_counts": rejection_counts,
            "pool_size": len(state["pool"]),
            "today_fresh": sum(
                1 for event in state["fresh_outbox"]
                if datetime.fromtimestamp(event["event_at"] / 1000, ZoneInfo("Asia/Shanghai")).date()
                == datetime.now(ZoneInfo("Asia/Shanghai")).date()
            ),
            "dropped_price_rows": self._dropped_price_rows,
        }
