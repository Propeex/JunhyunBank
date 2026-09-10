from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable

import websocket


class MarketStream:
    URL = "wss://api.upbit.com/websocket/v1"

    def __init__(
        self,
        markets: list[str],
        on_price: Callable[[str, float], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.markets = list(dict.fromkeys(markets))
        self.on_price = on_price
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="upbit-websocket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set() and self.markets:
            ws = None
            try:
                ws = websocket.create_connection(self.URL, timeout=10)
                request = [
                    {"ticket": str(uuid.uuid4())},
                    {"type": "ticker", "codes": self.markets, "is_only_realtime": True},
                    {"format": "DEFAULT"},
                ]
                ws.send(json.dumps(request))
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
                    market = data.get("code")
                    price = data.get("trade_price")
                    if market and price is not None and self.on_price:
                        self.on_price(str(market), float(price))
            except Exception as exc:
                if self.on_error and not self._stop.is_set():
                    self.on_error(f"WebSocket 재연결: {exc}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
