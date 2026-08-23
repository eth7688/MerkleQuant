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
    ):
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
        self._state_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []
        self._app = None
        self._running = False
        self._stream_connected = False
        self._stream_ever_connected = False
        self._price_stream_status = "stopped"
        self._last_scan_at = 0
        self._scan_started_at = 0
        self._scan_duration_ms = 0
        self._next_scan_at = None
        self._last_error = ""
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

    def _apply_prices(self, prices: dict[str, float], now_ms: int) -> None:
        if not prices:
            return
        with self._state_lock:
            with compression_state_lock(self.state_path):
                state = load_compression_state(self.state_path)
                state, events = apply_live_prices(state, prices, now_ms)
                save_compression_state(self.state_path, state)
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

    def handle_message(self, message, *, now_ms=None) -> None:
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
        for row in rows:
            if not isinstance(row, dict) or row.get("s") not in symbols:
                self._dropped_price_rows += 1
                continue
            price = self._finite_price(row.get("c"))
            if price is not None:
                prices[row["s"]] = price
            else:
                self._dropped_price_rows += 1
        self._apply_prices(prices, self.time_ms() if now_ms is None else now_ms)

    def rest_fallback_once(self, *, now_ms=None) -> bool:
        try:
            response = self.http_get(FUTURES_PRICE_URL, timeout=10)
            response.raise_for_status()
            rows = response.json()
        except Exception as error:
            self._last_error = f"REST fallback: {error}"
            return False
        symbols = self._pool_symbols()
        prices = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("symbol") not in symbols:
                continue
            price = self._finite_price(row.get("price"))
            if price is not None:
                prices[row["symbol"]] = price
        self._apply_prices(prices, self.time_ms() if now_ms is None else now_ms)
        return True

    def _on_open(self, app):
        self._stream_connected = True
        self._stream_ever_connected = True
        self._price_stream_status = "connected"

    def _on_message(self, app, message):
        self.handle_message(message)

    def _on_error(self, app, error):
        self._stream_connected = False
        self._last_error = f"WebSocket: {error}"

    def _on_close(self, app, *args):
        self._stream_connected = False
        if not self._stop.is_set():
            self._price_stream_status = "reconnecting"

    def _stream_loop(self):
        attempt = 0
        while not self._stop.is_set():
            if self.websocket_factory is None:
                self._last_error = "websocket-client dependency is unavailable"
                self._price_stream_status = "unavailable"
                return
            self._price_stream_status = "connecting"
            self._stream_ever_connected = False
            try:
                self._app = self.websocket_factory(
                    FUTURES_MINI_TICKER_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._app.run_forever()
            except Exception as error:
                self._stream_connected = False
                self._last_error = f"WebSocket: {error}"
            if self._stop.is_set():
                break
            self._price_stream_status = "reconnecting"
            if self._stream_ever_connected:
                attempt = 0
            self.sleep(self.reconnect_delay(attempt))
            attempt += 1

    def _fallback_loop(self):
        while not self._stop.is_set():
            if not self._stream_connected:
                self.rest_fallback_once()
            self.sleep(5)

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
            workers = (self._stream_loop, self._fallback_loop, self._scheduler_loop)
            self._active_worker_count = len(workers)
            self._threads = [
                self.thread_factory(
                    target=lambda worker=worker: self._run_worker(worker, generation),
                    name=name,
                    daemon=True,
                )
                for worker, name in zip(workers, (
                    "compression-price-stream", "compression-price-fallback", "compression-15m-scheduler",
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
        self._stream_connected = False
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
        structure_scanning = self._scan_lock.locked()
        return {
            "running": running,
            "auto_enabled": state["auto_enabled"],
            "structure_scanning": structure_scanning,
            "scan_started_at": self._scan_started_at,
            "scan_duration_ms": self._scan_duration_ms,
            "scan_overdue": structure_scanning and self._scan_started_at > 0
            and self.time_ms() - self._scan_started_at >= 900_000,
            "price_stream_status": self._price_stream_status,
            "last_scan_at": self._last_scan_at or state["last_structure_scan_at"],
            "next_scan_at": self._next_scan_at.isoformat() if self._next_scan_at else None,
            "last_error": self._last_error or state["last_error"],
            "pool_size": len(state["pool"]),
            "today_fresh": sum(
                1 for event in state["fresh_outbox"]
                if datetime.fromtimestamp(event["event_at"] / 1000, ZoneInfo("Asia/Shanghai")).date()
                == datetime.now(ZoneInfo("Asia/Shanghai")).date()
            ),
            "dropped_price_rows": self._dropped_price_rows,
        }
