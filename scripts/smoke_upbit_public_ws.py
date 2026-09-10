from __future__ import annotations

import argparse
import threading
import time

from junhyunbank.market_stream import MarketStream
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.upbit import UpbitClient


def _console_safe(value: str) -> str:
    # GitHub-hosted Windows PowerShell can expose a legacy single-byte console
    # encoding. Keep smoke diagnostics printable there even if an exception or
    # callback contains Korean/Unicode text.
    return value.encode("ascii", errors="backslashreplace").decode("ascii")


def _discover_safe_krw_market() -> tuple[str | None, str]:
    client = UpbitClient(timeout=10.0)
    try:
        rows = client.get_markets()
    except Exception as exc:
        client.close()
        return None, f"market discovery request failed: {exc}"

    raw_krw = []
    safe_krw = []
    flagged = 0
    unknown = 0
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue
            market = str(row.get("market") or "").strip().upper()
            if not market.startswith("KRW-"):
                continue
            raw_krw.append(market)
            is_flagged, understood = TradingEngine._market_alert_status(row)
            if is_flagged:
                flagged += 1
            elif understood:
                safe_krw.append(market)
            else:
                unknown += 1
    finally:
        client.close()

    if not raw_krw:
        return None, f"market discovery returned no KRW pairs; rows={len(rows)}"
    if not safe_krw:
        return None, (
            f"market discovery produced zero safe KRW pairs; raw={len(raw_krw)} "
            f"flagged={flagged} unknown={unknown}"
        )

    market = "KRW-BTC" if "KRW-BTC" in safe_krw else safe_krw[0]
    return market, (
        f"market discovery ok; rows={len(rows)} raw_krw={len(raw_krw)} "
        f"safe_krw={len(safe_krw)} flagged={flagged} unknown={unknown} "
        f"probe={market}"
    )


def run_once(timeout: float) -> tuple[bool, str]:
    market, discovery = _discover_safe_krw_market()
    if not market:
        return False, discovery

    got_trade = threading.Event()
    got_orderbook = threading.Event()
    errors: list[str] = []

    def on_trade(event):
        if event.get("code") == market and float(event.get("trade_price") or 0.0) > 0:
            got_trade.set()

    def on_orderbook(event):
        if event.get("code") == market and event.get("orderbook_units"):
            got_orderbook.set()

    def on_error(message: str):
        errors.append(str(message))

    stream = MarketStream(
        [market],
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
                return True, (
                    f"{discovery}; trade/orderbook received; "
                    f"messages={stream.message_count}"
                )
            time.sleep(0.10)
        detail = errors[-1] if errors else stream.last_error or "timeout"
        return False, (
            f"{discovery}; trade={got_trade.is_set()} "
            f"orderbook={got_orderbook.is_set()} connected={stream.connected} "
            f"messages={stream.message_count} error={detail}"
        )
    finally:
        stream.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Upbit market-discovery + public WebSocket smoke test"
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    attempts = max(1, args.attempts)
    for attempt in range(1, attempts + 1):
        ok, detail = run_once(max(5.0, args.timeout))
        print(_console_safe(f"[attempt {attempt}/{attempts}] {detail}"), flush=True)
        if ok:
            return 0
        if attempt < attempts:
            time.sleep(2.0)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
