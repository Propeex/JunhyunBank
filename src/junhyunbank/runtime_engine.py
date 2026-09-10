from __future__ import annotations

import time
from typing import Any

from .engine import TradingEngine as BaseTradingEngine
from .market_stream import MarketStream


class TradingEngine(BaseTradingEngine):
    """V3 runtime-hardened engine.

    The trading/order/risk implementation remains in the V2 base engine.  This
    layer changes only deep orderbook subscription lifecycle so candidate-set
    changes do not tear down a healthy WebSocket connection.
    """

    def _reset_orderbook_state(self, market: str) -> None:
        # A market can leave the deep set and return later. Mixing a new book
        # snapshot with old delta history would distort Book Pressure, so start
        # its orderbook-only state fresh on re-entry. Trade/momentum history is
        # intentionally preserved.
        books = getattr(self.strategy, "_books", None)
        if isinstance(books, dict):
            books.pop(market, None)
        history = getattr(self.strategy, "_book_history", None)
        if history is not None:
            try:
                history.pop(market, None)
            except (AttributeError, KeyError):
                pass

    def _restart_deep_stream(self, markets: list[str]) -> None:
        markets = sorted(dict.fromkeys(markets))
        if markets == self._deep_markets:
            return

        previous = set(self._deep_markets)
        current = set(markets)
        added = sorted(current - previous)
        removed = sorted(previous - current)

        for market in added:
            self._reset_orderbook_state(market)

        self._deep_markets = markets
        now = time.monotonic()
        self._deep_entered_at = {
            market: self._deep_entered_at.get(market, now)
            for market in markets
        }

        if not markets:
            if self._deep_stream:
                self._deep_stream.stop()
                self._deep_stream = None
        elif self._deep_stream is None:
            self._deep_stream = MarketStream(
                markets,
                on_orderbook=self._on_orderbook,
                on_error=lambda message: self._emit(
                    "warning", f"호가 스트림: {message}"
                ),
                on_status=self._stream_status,
                orderbook_depth=max(5, self.config.strategy.orderbook_depth),
                name="upbit-orderbook-deep",
            )
            self._deep_stream.start()
        else:
            # Upbit supports replacing the active subscription by sending a new
            # subscription message on the existing socket. MarketStream keeps
            # the new list for the next reconnect even if this send races with
            # a reconnect, so there is no need to open a second connection.
            self._deep_stream.update_markets(markets)

        if added or removed:
            self.events.put(
                {
                    "type": "deep_set",
                    "added": added,
                    "removed": removed,
                    "count": len(markets),
                }
            )
