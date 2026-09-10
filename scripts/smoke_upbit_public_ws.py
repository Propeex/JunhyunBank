from __future__ import annotations

import argparse
import sys
import threading
import time

from junhyunbank.market_stream import MarketStream


def run_once(timeout: float) -> tuple[bool, str]:
    got_trade = threading.Event()
    got_orderbook = threading.Event()
    errors: list[str] = []

    def on_trade(event):
        if event.get("code") == "KRW-BTC" and float(event.get("trade_price") or 0.0) > 0:
            got_trade.set()

    def on_orderbook(event):
        if event.get("code") == "KRW-BTC" and event.get("orderbook_units"):
            got_orderbook.set()

    def on_error(message: str):
        errors.append(str(message))

    stream = MarketStream(
        ["KRW-BTC"],
        on_trade=on_trade,
        on_orderbook=on_orderbook,
        on_error=on_error,
        orderbook_depth=5,
        name="smoke-upbit-public",
    )
    stream.start()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if got_trade.is_set() and got_orderbook.is_set():
                return True, f"trade/orderbook 수신 성공 · messages={stream.message_count}"
            time.sleep(0.10)
        detail = errors[-1] if errors else stream.last_error or "timeout"
        return False, (
            f"trade={got_trade.is_set()} orderbook={got_orderbook.is_set()} "
            f"connected={stream.connected} messages={stream.message_count} error={detail}"
        )
    finally:
        stream.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Upbit public WebSocket smoke test")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    attempts = max(1, args.attempts)
    for attempt in range(1, attempts + 1):
        ok, detail = run_once(max(5.0, args.timeout))
        print(f"[attempt {attempt}/{attempts}] {detail}", flush=True)
        if ok:
            return 0
        if attempt < attempts:
            time.sleep(2.0)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
