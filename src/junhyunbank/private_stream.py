from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket


class PrivateAccountStream:
    """Authenticated Upbit myOrder + myAsset stream.

    Private account streams are event-driven: silence is normal when there are
    no order/fill/balance changes. Unlike public market data, lack of messages
    must therefore never be interpreted as stale data. We keep the connection
    alive with WebSocket ping frames and use reconnect/backoff only when the
    socket itself fails.

    The authorization callback must return a complete ``Bearer ...`` value and
    is called for every new connection so a fresh JWT nonce is used. The token
    is never included in status/error callbacks.
    """

    URL = "wss://api.upbit.com/websocket/v1/private"
    _connect_lock = threading.Lock()
    _last_connect_attempt = 0.0
    _min_connect_interval = 0.25

    def __init__(
        self,
        authorization_factory: Callable[[], str],
        *,
        on_order: Callable[[dict[str, Any]], None] | None = None,
        on_asset: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        name: str = "upbit-private-account",
        ping_interval: float = 30.0,
    ) -> None:
        self.authorization_factory = authorization_factory
        self.on_order = on_order
        self.on_asset = on_asset
        self.on_error = on_error
        self.on_status = on_status
        self.name = name
        self.ping_interval = max(10.0, min(float(ping_interval), 60.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._message_count = 0
        self._order_count = 0
        self._asset_count = 0
        self._connected_at = 0.0
        self._last_order = 0.0
        self._last_asset = 0.0
        self._last_pong = 0.0
        self._last_error = ""
        self._ws: Any | None = None
        self._ws_lock = threading.RLock()
        self._active_auth = ""

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def order_count(self) -> int:
        return self._order_count

    @property
    def asset_count(self) -> int:
        return self._asset_count

    @property
    def last_error(self) -> str:
        return self._last_error

    @staticmethod
    def _age(value: float) -> float:
        if value <= 0:
            return float("inf")
        return max(0.0, time.monotonic() - value)

    @property
    def order_age_seconds(self) -> float:
        return self._age(self._last_order)

    @property
    def asset_age_seconds(self) -> float:
        return self._age(self._last_asset)

    @property
    def connected_age_seconds(self) -> float:
        return self._age(self._connected_at)

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
            self._thread.join(timeout=2.0)

    @staticmethod
    def _subscription() -> list[dict[str, Any]]:
        # myOrder may omit codes to subscribe to all markets. myAsset must not
        # contain codes; Upbit returns WRONG_FORMAT if it does.
        return [
            {"ticket": str(uuid.uuid4())},
            {"type": "myOrder"},
            {"type": "myAsset"},
            {"format": "DEFAULT"},
        ]

    @classmethod
    def _wait_for_connect_slot(cls) -> None:
        with cls._connect_lock:
            now = time.monotonic()
            delay = cls._min_connect_interval - (now - cls._last_connect_attempt)
            if delay > 0:
                time.sleep(delay)
            cls._last_connect_attempt = time.monotonic()

    @staticmethod
    def _server_error(data: dict[str, Any]) -> str | None:
        error = data.get("error")
        if not isinstance(error, dict):
            return None
        name = str(error.get("name") or "websocket_error")
        message = str(error.get("message") or "WebSocket 요청에 실패했습니다.")
        return f"{name}: {message}"

    def _redact(self, value: object) -> str:
        text = str(value)
        auth = self._active_auth
        if auth:
            text = text.replace(auth, "<authorization-redacted>")
            if auth.lower().startswith("bearer "):
                token = auth[7:]
                if token:
                    text = text.replace(token, "<jwt-redacted>")
        return text

    def _emit_status(self, state: str, message: str = "") -> None:
        if not self.on_status:
            return
        try:
            self.on_status(
                {
                    "name": self.name,
                    "state": state,
                    "connected": self._connected,
                    "messages": self._message_count,
                    "order_events": self._order_count,
                    "asset_events": self._asset_count,
                    "connected_age_seconds": self.connected_age_seconds,
                    "order_age_seconds": self.order_age_seconds,
                    "asset_age_seconds": self.asset_age_seconds,
                    "message": self._redact(message),
                }
            )
        except Exception:
            pass

    def _open_connection(self):
        self._wait_for_connect_slot()
        authorization = str(self.authorization_factory() or "").strip()
        if not authorization.lower().startswith("bearer "):
            raise RuntimeError("Private WebSocket Authorization 생성 실패")
        self._active_auth = authorization
        # Suppress Origin for consistency with the public desktop WebSockets.
        # A fresh JWT is generated on every reconnect through the callback.
        return websocket.create_connection(
            self.URL,
            timeout=self.ping_interval,
            header=[f"Authorization: {authorization}"],
            suppress_origin=True,
        )

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                self._emit_status("connecting")
                ws = self._open_connection()
                with self._ws_lock:
                    self._ws = ws
                    self._connected = True
                ws.send(json.dumps(self._subscription()))
                self._connected_at = time.monotonic()
                self._last_pong = self._connected_at
                self._last_error = ""
                self._emit_status("connected")
                backoff = 1.0

                while not self._stop.is_set():
                    try:
                        payload = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # Private streams may legitimately stay silent for a long
                        # time. Ping keeps the 120-second Upbit idle timeout away.
                        ws.ping()
                        self._last_pong = time.monotonic()
                        self._emit_status("alive")
                        continue
                    if payload in (None, "", b""):
                        raise RuntimeError("Private WebSocket 연결이 종료되었습니다.")
                    if isinstance(payload, bytes):
                        payload = payload.decode("utf-8")
                    data = json.loads(payload)
                    if not isinstance(data, dict):
                        continue
                    server_error = self._server_error(data)
                    if server_error:
                        raise RuntimeError(f"Upbit Private WebSocket {server_error}")
                    self._message_count += 1
                    message_type = str(data.get("type") or "")
                    if message_type == "myOrder":
                        self._order_count += 1
                        self._last_order = time.monotonic()
                        if self.on_order:
                            self.on_order(data)
                    elif message_type == "myAsset":
                        self._asset_count += 1
                        self._last_asset = time.monotonic()
                        if self.on_asset:
                            self.on_asset(data)
            except Exception as exc:
                self._connected = False
                safe = self._redact(exc)
                self._last_error = safe
                self._emit_status("reconnecting", safe)
                if self.on_error and not self._stop.is_set():
                    try:
                        self.on_error(f"Private WebSocket 재연결: {safe}")
                    except Exception:
                        pass
                self._stop.wait(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                with self._ws_lock:
                    self._connected = False
                    if self._ws is ws:
                        self._ws = None
                self._active_auth = ""
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        self._emit_status("stopped")
