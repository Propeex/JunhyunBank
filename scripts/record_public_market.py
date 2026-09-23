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
from junhyunbank.config import SafetyConfig, StrategyConfig
from junhyunbank.market_stream import MarketStream
from junhyunbank.recorder import (
    MarketDataRecorder,
    RecorderConfig,
    recording_integrity_ok,
    recording_integrity_reasons,
    subscription_metadata_changed,
)
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.strategy import MicroFlowStrategy
from junhyunbank.upbit import UpbitClient
from junhyunbank.validation import rotating_control_markets


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
    parser.add_argument(
        "--orderbook-controls",
        type=int,
        default=4,
        help="Hot 후보 밖에서 결과와 무관하게 순환 수집할 대조군 수",
    )
    parser.add_argument("--control-rotate-seconds", type=float, default=60.0)
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
    orderbook_controls = max(
        0, min(100 - orderbook_top, int(args.orderbook_controls))
    )
    orderbook_requested = orderbook_top > 0 or orderbook_controls > 0
    control_rotate_seconds = max(10.0, float(args.control_rotate_seconds))
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
        markets = TradingEngine.validated_initial_markets(rows, SafetyConfig())

        recorder.start()
        if not recorder.record_meta(
            "session_start",
            junhyunbank_version=__version__,
            safe_krw_markets=markets,
            orderbook_top=orderbook_top,
            orderbook_controls=orderbook_controls,
            control_rotate_seconds=control_rotate_seconds,
            orderbook_depth=orderbook_depth,
            candidate_refresh_seconds=candidate_refresh,
            strategy_config=asdict(strategy.config),
            orders_submitted=0,
        ):
            raise RuntimeError("session_start metadata를 기록하지 못했습니다.")

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
        next_control_rotation = 0.0
        control_cursor = 0
        control_markets: list[str] = []
        candidate_markets: list[str] = []
        candidate_entered_at: dict[str, float] = {}
        current_hot: set[str] = set()
        current_controls: set[str] = set()
        while not stopping.is_set():
            now = time.monotonic()
            if duration > 0 and now - started >= duration:
                break

            if (orderbook_top > 0 or orderbook_controls > 0) and now >= next_candidates:
                with strategy_lock:
                    ranked = strategy.rank_markets(
                        markets, strategy.config.scanner_candidate_count
                    )
                    selection = strategy.select_actionable_deep_markets(
                        markets,
                        ranked,
                        current_deep=candidate_markets,
                        entered_at=candidate_entered_at,
                        now=now,
                        stale_limit=MarketStream.DEFAULT_MAX_EVENT_LAG_SECONDS,
                        scanner_limit=strategy.config.scanner_candidate_count,
                        deep_limit=orderbook_top,
                    )
                hot_markets = selection.selected
                candidate_entered_at = {
                    market: candidate_entered_at.get(market, now)
                    for market in hot_markets
                }
                candidate_markets = list(hot_markets)
                retained_controls = [
                    market for market in control_markets if market not in hot_markets
                ]
                if (
                    now >= next_control_rotation
                    or len(retained_controls) < orderbook_controls
                ):
                    control_markets, control_cursor = rotating_control_markets(
                        markets,
                        excluded=hot_markets,
                        count=orderbook_controls,
                        cursor=control_cursor,
                    )
                    next_control_rotation = now + control_rotate_seconds
                else:
                    control_markets = retained_controls
                desired = sorted(dict.fromkeys(hot_markets + control_markets))
                next_hot = set(hot_markets)
                next_controls = set(control_markets)
                subscription_changed = desired != current_deep
                metadata_changed = subscription_metadata_changed(
                    current_markets=current_deep,
                    current_hot=current_hot,
                    current_controls=current_controls,
                    next_markets=desired,
                    next_hot=next_hot,
                    next_controls=next_controls,
                )
                if metadata_changed:
                    # The metadata gets the lower recorder sequence before the
                    # stream/role map is changed. Replay can then reset book
                    # continuity before consuming an event under the new role.
                    if not recorder.record_meta(
                        "orderbook_subscription",
                        markets=desired,
                        hot_markets=hot_markets,
                        control_markets=control_markets,
                        scores={
                            market: score
                            for market, score in selection.actionable_ranked
                            if market in hot_markets
                        },
                        stale_skipped=selection.stale_skipped,
                        supplemented=selection.supplemented,
                    ):
                        errors["recorder_subscription_metadata_drop"] += 1
                        stopping.set()
                        continue
                    if subscription_changed:
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
                    current_hot = next_hot
                    current_controls = next_controls
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
                            "control_markets": len(control_markets),
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

        # Establish a hard session boundary: all callback producers are joined
        # before the final quality snapshot is enqueued.
        for stream in trade_streams:
            stream.stop()
        trade_streams = []
        if deep_stream is not None:
            deep_stream.stop()
            deep_stream = None
        recorder.record_session_end(
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
    integrity_kwargs = {
        "stopped_cleanly": stopped_cleanly,
        "dropped": stats.dropped,
        "last_error": stats.last_error,
        "websocket_errors": dict(errors),
        "trade_events": stats.trade_events,
        "orderbook_events": stats.orderbook_events,
        "orderbook_requested": orderbook_requested,
    }
    integrity_ok = recording_integrity_ok(
        **integrity_kwargs,
    )
    integrity_reasons = recording_integrity_reasons(
        **integrity_kwargs,
    )
    result = {
        "output_dir": str(Path(args.output_dir).expanduser()),
        "clean_stop": stopped_cleanly,
        "enqueued": stats.enqueued,
        "written": stats.written,
        "trade_events": stats.trade_events,
        "orderbook_events": stats.orderbook_events,
        "orderbook_requested": orderbook_requested,
        "dropped": stats.dropped,
        "segments": stats.segments_finalized,
        "compressed_segments": stats.compressed_segments,
        "recovered_parts": stats.recovered_parts,
        "last_error": stats.last_error,
        "websocket_errors": dict(errors),
        "data_integrity_ok": integrity_ok,
        "integrity_reasons": integrity_reasons,
        "orders_submitted": 0,
    }
    print(json.dumps(result, ensure_ascii=True), flush=True)

    # The recording remains usable for inspection, but a nonzero status makes
    # automation refuse to silently promote an incomplete dataset to replay.
    return 0 if integrity_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
