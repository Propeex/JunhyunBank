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
        self._last_error = ""
        self._ws: Any | None = None
        self._ws_lock = threading.RLock()

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
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _subscription(self) -> list[dict[str, Any]]:
        request: list[dict[str, Any]] = [{"ticket": str(uuid.uuid4())}]
        if self.on_trade:
            request.append(
                {
                    "type": "trade",
                    "codes": list(self.markets),
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

    def _send_subscription(self) -> bool:
        with self._ws_lock:
            ws = self._ws
            if ws is None or not self._connected:
                return False
            try:
                ws.send(json.dumps(self._subscription()))
                return True
            except Exception as exc:
                self._last_error = str(exc)
                try:
                    ws.close()
                except Exception:
                    pass
                return False

    def update_markets(self, markets: list[str]) -> bool:
        """Update a live subscription without reconnecting when possible.

        Upbit supports replacing the active data subscription by sending a new
        subscription message on the existing WebSocket. If the connection is
        unavailable, the new market list is retained and used on reconnect.
        """
        updated = list(dict.fromkeys(markets))
        if updated == self.markets:
            return self._connected
        self.markets = updated
        sent = self._send_subscription() if updated else False
        self._emit_status("subscribed" if sent else "subscription_pending")
        return sent

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

    @staticmethod
    def _server_error(data: dict[str, Any]) -> str | None:
        error = data.get("error")
        if not isinstance(error, dict):
            return None
        name = str(error.get("name") or "websocket_error")
        message = str(error.get("message") or "WebSocket 요청에 실패했습니다.")
        return f"{name}: {message}"

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set() and self.markets:
            ws = None
            try:
                self._emit_status("connecting")
                ws = self._open_connection()
                with self._ws_lock:
                    self._ws = ws
                    self._connected = True
                if not self._send_subscription():
                    raise RuntimeError("WebSocket 구독 요청 전송 실패")
                self._last_error = ""
                connected_at = time.monotonic()
                self._emit_status("connected")
                backoff = 1.0
                while not self._stop.is_set():
                    try:
                        payload = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - max(connected_at, self._last_message) >= 30.0:
                            raise RuntimeError('30초 이상 시세 수신 없음 · 연결 복구')
                        ws.ping()
                        continue
                    if payload in (None, "", b""):
                        raise RuntimeError("WebSocket 연결이 종료되었습니다.")
                    if isinstance(payload, bytes):
                        payload = payload.decode("utf-8")
                    data = json.loads(payload)
                    if not isinstance(data, dict):
                        continue
                    server_error = self._server_error(data)
                    if server_error:
                        raise RuntimeError(f"Upbit WebSocket {server_error}")
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
                with self._ws_lock:
                    self._connected = False
                    if self._ws is ws:
                        self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        self._emit_status("stopped")
