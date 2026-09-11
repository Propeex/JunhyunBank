"""Record Upbit public trades and dynamic candidate L2 order books for replay.

This research command creates ``UpbitClient()`` without credentials, subscribes
only to public WebSockets, and never calls an order endpoint. All disk I/O is
performed by ``MarketDataRecorder`` background workers so WebSocket callbacks
never wait for file writes or compression.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import signal
import threading
import time
from typing import Any

from junhyunbank import __version__
from junhyunbank.config import StrategyConfig
from junhyunbank.market_stream import MarketStream
from junhyunbank.recorder import MarketDataRecorder, RecorderConfig
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.strategy import MicroFlowStrategy
from junhyunbank.upbit import UpbitClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / ".junhyunbank" / "recordings",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=3600.0,
        help="0이면 Ctrl+C까지 계속 기록합니다.",
    )
    parser.add_argument("--orderbook-top", type=int, default=24)
    parser.add_argument("--orderbook-depth", type=int, default=15)
    parser.add_argument("--candidate-refresh", type=float, default=3.0)
    parser.add_argument("--queue-max", type=int, default=50_000)
    parser.add_argument("--rotate-mb", type=float, default=64.0)
    parser.add_argument("--rotate-minutes", type=float, default=15.0)
    parser.add_argument("--max-gb", type=float, default=5.0)
    parser.add_argument("--max-files", type=int, default=512)
    parser.add_argument("--no-compress", action="store_true")
    args = parser.parse_args()

    duration = max(0.0, float(args.seconds))
    orderbook_top = max(0, min(100, int(args.orderbook_top)))
    orderbook_depth = max(1, min(30, int(args.orderbook_depth)))
    candidate_refresh = max(0.5, float(args.candidate_refresh))
    rotate_bytes = max(1, int(float(args.rotate_mb) * 1024 * 1024))
    rotate_seconds = max(10.0, float(args.rotate_minutes) * 60.0)
    max_total_bytes = max(1, int(float(args.max_gb) * 1024**3))

    recorder = MarketDataRecorder(
        RecorderConfig(
            directory=args.output_dir,
            prefix="upbit-public",
            queue_max=max(100, int(args.queue_max)),
            rotate_bytes=rotate_bytes,
            rotate_seconds=rotate_seconds,
            compress=not args.no_compress,
            max_total_bytes=max_total_bytes,
            max_files=max(1, int(args.max_files)),
        )
    )
    strategy = MicroFlowStrategy(StrategyConfig())
    client = UpbitClient()
    strategy_lock = threading.RLock()
    errors: Counter[str] = Counter()
    stopping = threading.Event()

    def request_stop(*_: Any) -> None:
        stopping.set()

    try:
        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)
    except ValueError:
        # signal() is only legal from the main thread. This keeps unit/invoked
        # use safe even if main() is called from another thread.
        pass

    def stream_error(message: str) -> None:
        text = str(message)
        errors[text] += 1
        recorder.record_meta("websocket_error", message=text)

    def on_trade(event: dict[str, Any]) -> None:
        received_ns = time.time_ns()
        received_mono_ns = time.monotonic_ns()
        recorder.record_trade(
            event,
            received_ns=received_ns,
            received_monotonic_ns=received_mono_ns,
        )
        with strategy_lock:
            strategy.on_trade(event)

    def on_orderbook(event: dict[str, Any]) -> None:
        recorder.record_orderbook(
            event,
            received_ns=time.time_ns(),
            received_monotonic_ns=time.monotonic_ns(),
        )

    trade_streams: list[MarketStream] = []
    deep_stream: MarketStream | None = None
    current_deep: list[str] = []
    started = time.monotonic()

    try:
        rows = client.get_markets()
        markets = sorted(
            str(row.get("market") or "")
            for row in rows
            if isinstance(row, dict)
            and str(row.get("market") or "").startswith("KRW-")
            and not TradingEngine._is_warning_market(row)
        )
        if not markets:
            raise RuntimeError(
                "안전하게 해석 가능한 Upbit KRW public market이 없습니다."
            )

        recorder.start()
        recorder.record_meta(
            "session_start",
            junhyunbank_version=__version__,
            safe_krw_markets=markets,
            orderbook_top=orderbook_top,
            orderbook_depth=orderbook_depth,
            candidate_refresh_seconds=candidate_refresh,
            strategy_config=asdict(strategy.config),
            orders_submitted=0,
        )

        trade_streams = [
            MarketStream(
                markets[index : index + 100],
                on_trade=on_trade,
                on_error=stream_error,
                name=f"recorder-trades-{index // 100 + 1}",
            )
            for index in range(0, len(markets), 100)
        ]
        for stream in trade_streams:
            stream.start()

        next_candidates = 0.0
        next_status = 0.0
        while not stopping.is_set():
            now = time.monotonic()
            if duration > 0 and now - started >= duration:
                break

            if orderbook_top > 0 and now >= next_candidates:
                with strategy_lock:
                    ranked = strategy.rank_markets(markets, orderbook_top)
                desired = sorted(market for market, _ in ranked)
                if desired != current_deep:
                    if deep_stream is None and desired:
                        deep_stream = MarketStream(
                            desired,
                            on_orderbook=on_orderbook,
                            on_error=stream_error,
                            orderbook_depth=orderbook_depth,
                            name="recorder-orderbook",
                        )
                        deep_stream.start()
                    elif deep_stream is not None and desired:
                        deep_stream.update_markets(desired)
                    elif deep_stream is not None:
                        deep_stream.stop()
                        deep_stream = None
                    current_deep = desired
                    recorder.record_meta(
                        "orderbook_subscription",
                        markets=current_deep,
                        scores={market: score for market, score in ranked},
                    )
                next_candidates = now + candidate_refresh

            if now >= next_status:
                stats = recorder.stats()
                with strategy_lock:
                    warmed = sum(
                        strategy.warmup_ratio(market) >= 1.0
                        for market in markets
                    )
                print(
                    json.dumps(
                        {
                            "elapsed": round(now - started, 1),
                            "markets": len(markets),
                            "warmed": warmed,
                            "orderbook_markets": len(current_deep),
                            "enqueued": stats.enqueued,
                            "written": stats.written,
                            "dropped": stats.dropped,
                            "queue_depth": stats.queue_depth,
                            "segments": stats.segments_finalized,
                            "ws_errors": sum(errors.values()),
                            "orders_submitted": 0,
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                next_status = now + 5.0
            stopping.wait(0.05)

        recorder.record_meta(
            "session_end",
            elapsed_seconds=max(0.0, time.monotonic() - started),
            websocket_errors=dict(errors),
            orders_submitted=0,
        )
    finally:
        for stream in trade_streams:
            stream.stop()
        if deep_stream is not None:
            deep_stream.stop()
        client.close()
        stopped_cleanly = recorder.stop(timeout=20.0)

    stats = recorder.stats()
    result = {
        "output_dir": str(Path(args.output_dir).expanduser()),
        "clean_stop": stopped_cleanly,
        "enqueued": stats.enqueued,
        "written": stats.written,
        "dropped": stats.dropped,
        "segments": stats.segments_finalized,
        "compressed_segments": stats.compressed_segments,
        "recovered_parts": stats.recovered_parts,
        "last_error": stats.last_error,
        "websocket_errors": dict(errors),
        "orders_submitted": 0,
    }
    print(json.dumps(result, ensure_ascii=True), flush=True)

    # The recording remains usable for inspection, but a nonzero status makes
    # automation refuse to silently promote an incomplete dataset to replay.
    return 0 if stopped_cleanly and stats.dropped == 0 and not stats.last_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
