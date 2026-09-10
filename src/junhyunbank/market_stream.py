from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket


class MarketStream:
    URL = "wss://api.upbit.com/websocket/v1"
    _connect_lock = threading.Lock()
    _last_connect_attempt = 0.0
    _min_connect_interval = 0.25

    def __init__(
        self,
        markets: list[str],
        *,
        on_trade: Callable[[dict[str, Any]], None] | None = None,
        on_orderbook: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        orderbook_depth: int = 5,
        name: str = "upbit-websocket",
    ) -> None:
        self.markets = list(dict.fromkeys(markets))
        self.on_trade = on_trade
        self.on_orderbook = on_orderbook
        self.on_error = on_error
        self.on_status = on_status
        self.orderbook_depth = orderbook_depth
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_message = 0.0
        self._connected = False
        self._message_count = 0
        self._connected_at = 0.0
        self._last_error = ""

    @property
    def age_seconds(self) -> float:
        if self._last_message <= 0:
            return float("inf")
        return max(0.0, time.monotonic() - self._last_message)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _subscription(self) -> list[dict[str, Any]]:
        request: list[dict[str, Any]] = [{"ticket": str(uuid.uuid4())}]
        if self.on_trade:
            request.append(
                {
                    "type": "trade",
                    "codes": self.markets,
                    "is_only_realtime": True,
                }
            )
        if self.on_orderbook:
            depth = max(1, min(30, int(self.orderbook_depth)))
            request.append(
                {
                    "type": "orderbook",
                    "codes": [f"{market}.{depth}" for market in self.markets],
                    "is_only_realtime": True,
                }
            )
        request.append({"format": "DEFAULT"})
        return request

    def _emit_status(self, state: str, message: str = "") -> None:
        if not self.on_status:
            return
        try:
            self.on_status(
                {
                    "name": self.name,
                    "state": state,
                    "connected": self._connected,
                    "markets": len(self.markets),
                    "messages": self._message_count,
                    "age_seconds": self.age_seconds,
                    "message": message,
                }
            )
        except Exception:
            pass

    @classmethod
    def _wait_for_connect_slot(cls) -> None:
        with cls._connect_lock:
            now = time.monotonic()
            delay = cls._min_connect_interval - (now - cls._last_connect_attempt)
            if delay > 0:
                time.sleep(delay)
            cls._last_connect_attempt = time.monotonic()

    def _open_connection(self):
        self._wait_for_connect_slot()
        # websocket-client adds Origin by default. Upbit applies a much stricter
        # public API limit to requests carrying Origin, so desktop clients must
        # suppress it explicitly.
        return websocket.create_connection(
            self.URL,
            timeout=10,
            suppress_origin=True,
        )

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set() and self.markets:
            ws = None
            try:
                self._emit_status("connecting")
                ws = self._open_connection()
                ws.send(json.dumps(self._subscription()))
                self._connected = True
                self._connected_at = time.monotonic()
                self._last_error = ""
                self._emit_status("connected")
                backoff = 1.0
                while not self._stop.is_set():
                    try:
                        payload = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping()
                        continue
                    if isinstance(payload, bytes):
                        payload = payload.decode("utf-8")
                    data = json.loads(payload)
                    if not isinstance(data, dict):
                        continue
                    self._last_message = time.monotonic()
                    self._message_count += 1
                    message_type = str(data.get("type") or "")
                    if message_type == "trade" and self.on_trade:
                        self.on_trade(data)
                    elif message_type == "orderbook" and self.on_orderbook:
                        self.on_orderbook(data)
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                self._emit_status("reconnecting", self._last_error)
                if self.on_error and not self._stop.is_set():
                    self.on_error(f"WebSocket 재연결: {exc}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)
            finally:
                self._connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        self._emit_status("stopped")
