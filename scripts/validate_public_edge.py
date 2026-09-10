"""Read-only live forward-edge validation for the current JH-MicroFlow entry logic.

This command never loads API credentials and never submits orders. It warms the
real strategy with Upbit public trades, subscribes to order books for current
candidates, records the strategy decision/features at executable top-of-book,
and labels those observations later with future bid/ask quotes.

The label pays entry ask -> future bid spread plus both assumed fees. It is still
size-free: depth slippage, queue position and real order latency are not modeled,
so the output must not be treated as a profitability guarantee.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from junhyunbank.config import StrategyConfig
from junhyunbank.market_stream import MarketStream
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.strategy import MicroFlowStrategy
from junhyunbank.upbit import UpbitClient
from junhyunbank.validation import (
    absolute_mid_move,
    net_round_trip_return,
    reason_counts,
    summarize_samples,
)


def _parse_horizons(value: str) -> list[int]:
    result = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    if not result or any(item <= 0 for item in result):
        raise argparse.ArgumentTypeError("horizons는 양의 초 단위 정수 목록이어야 합니다.")
    return result


def _safe_quote(event: dict[str, Any]) -> tuple[float, float] | None:
    units = event.get("orderbook_units") or []
    if not isinstance(units, list) or not units or not isinstance(units[0], dict):
        return None
    try:
        bid = float(units[0].get("bid_price") or 0.0)
        ask = float(units[0].get("ask_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask <= 0 or bid > ask:
        return None
    return bid, ask


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=900.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--horizons", type=_parse_horizons, default=_parse_horizons("30,60,120,300"))
    parser.add_argument("--sample-every", type=float, default=10.0)
    parser.add_argument("--top", type=int, default=24)
    parser.add_argument("--assumed-fee", type=float, default=0.0005)
    parser.add_argument("--max-book-markets", type=int, default=80)
    args = parser.parse_args()

    seconds = max(30.0, float(args.seconds))
    horizons = list(args.horizons)
    max_horizon = max(horizons)
    sample_every = max(1.0, float(args.sample_every))
    top_count = max(1, min(50, int(args.top)))
    max_book_markets = max(top_count, min(100, int(args.max_book_markets)))
    fee = float(args.assumed_fee)
    if not (0 <= fee < 0.05):
        raise SystemExit("--assumed-fee는 0 이상 0.05 미만이어야 합니다.")

    client = UpbitClient()
    strategy = MicroFlowStrategy(StrategyConfig())
    lock = threading.RLock()
    errors: Counter[str] = Counter()
    quotes: dict[str, dict[str, float]] = {}
    samples: list[dict[str, Any]] = []
    last_sample: dict[str, float] = {}
    dropped_new_samples = 0

    def trade(event: dict[str, Any]) -> None:
        with lock:
            strategy.on_trade(event)

    def book(event: dict[str, Any]) -> None:
        quote = _safe_quote(event)
        with lock:
            strategy.on_orderbook(event)
            if quote is not None:
                market = str(event.get("code") or "")
                if market:
                    quotes[market] = {
                        "bid": quote[0],
                        "ask": quote[1],
                        "received_mono": time.monotonic(),
                        "epoch_ms": float(event.get("timestamp") or time.time() * 1000),
                    }

    def error(message: str) -> None:
        errors[str(message)] += 1

    rows = client.get_markets()
    markets = sorted(
        row["market"]
        for row in rows
        if isinstance(row, dict)
        and str(row.get("market") or "").startswith("KRW-")
        and not TradingEngine._is_warning_market(row)
    )
    if not markets:
        client.close()
        raise SystemExit("안전하게 해석 가능한 KRW public market이 없습니다.")

    streams = [
        MarketStream(markets[index : index + 100], on_trade=trade, on_error=error)
        for index in range(0, len(markets), 100)
    ]
    deep: MarketStream | None = None
    current_deep: list[str] = []
    started = time.monotonic()
    started_epoch_ms = int(time.time() * 1000)
    sampling_deadline = started + max(0.0, seconds - max_horizon)

    def unresolved_markets(now: float) -> list[str]:
        due_by_market: dict[str, float] = {}
        for sample in samples:
            labels = sample["labels"]
            for horizon in horizons:
                if str(horizon) in labels:
                    continue
                due = float(sample["created_mono"]) + horizon
                market = str(sample["market"])
                due_by_market[market] = min(due_by_market.get(market, due), due)
        return [market for market, _ in sorted(due_by_market.items(), key=lambda item: (item[1], item[0]))]

    def desired_book_markets(selected: list[str], now: float) -> list[str]:
        # Keep current candidates and enough unresolved markets subscribed so
        # future labels use executable bid/ask rather than a trade-price proxy.
        result: list[str] = []
        for market in selected + unresolved_markets(now):
            if market not in result:
                result.append(market)
            if len(result) >= max_book_markets:
                break
        return sorted(result)

    def label_due_samples(now: float) -> None:
        for sample in samples:
            quote = quotes.get(str(sample["market"]))
            if not quote:
                continue
            quote_mono = float(quote["received_mono"])
            for horizon in horizons:
                key = str(horizon)
                if key in sample["labels"]:
                    continue
                due = float(sample["created_mono"]) + horizon
                if now < due or quote_mono < due:
                    continue
                net = net_round_trip_return(
                    float(sample["entry_ask"]),
                    float(quote["bid"]),
                    bid_fee=fee,
                    ask_fee=fee,
                )
                move = absolute_mid_move(
                    float(sample["entry_bid"]),
                    float(sample["entry_ask"]),
                    float(quote["bid"]),
                    float(quote["ask"]),
                )
                if net is None or move is None:
                    continue
                sample["labels"][key] = {
                    "future_bid": quote["bid"],
                    "future_ask": quote["ask"],
                    "future_epoch_ms": int(quote["epoch_ms"]),
                    "label_delay_seconds": max(0.0, quote_mono - due),
                    "net_return": net,
                    "absolute_mid_move": move,
                }

    try:
        for stream in streams:
            stream.start()

        next_scan = 0.0
        while time.monotonic() - started < seconds:
            now = time.monotonic()
            if now >= next_scan:
                with lock:
                    ranked = strategy.rank_markets(markets, top_count)
                    selected = [market for market, _ in ranked]
                    desired = desired_book_markets(selected, now)
                    if desired != current_deep:
                        if deep is None and desired:
                            deep = MarketStream(
                                desired,
                                on_orderbook=book,
                                on_error=error,
                                orderbook_depth=max(5, strategy.config.orderbook_depth),
                                name="edge-validator-orderbook",
                            )
                            deep.start()
                        elif deep is not None and desired:
                            deep.update_markets(desired)
                        elif deep is not None and not desired:
                            deep.stop()
                            deep = None
                        current_deep = desired

                    regime, regime_factor = strategy.market_regime(markets)
                    warmed = sum(strategy.warmup_ratio(market) >= 1.0 for market in markets)
                    if now <= sampling_deadline:
                        for market in selected:
                            if now - last_sample.get(market, 0.0) < sample_every:
                                continue
                            if market not in current_deep:
                                dropped_new_samples += 1
                                continue
                            quote = quotes.get(market)
                            if not quote or now - float(quote["received_mono"]) > 3.0:
                                continue
                            features = strategy._feature_set(market)
                            if not features:
                                continue
                            decision = strategy.evaluate_entry(
                                market,
                                bid_fee=fee,
                                ask_fee=fee,
                                health=1.0,
                                regime_factor=regime_factor,
                            )
                            signal = decision.signal.value if regime_factor > 0 else "HOLD"
                            reason = decision.reason if regime_factor > 0 else "시장 PANIC · 신규 매수 차단"
                            samples.append(
                                {
                                    "created_mono": now,
                                    "created_epoch_ms": int(time.time() * 1000),
                                    "market": market,
                                    "signal": signal,
                                    "raw_signal": decision.signal.value,
                                    "kind": decision.kind.value,
                                    "score": decision.score,
                                    "reason": reason,
                                    "regime": regime,
                                    "regime_factor": regime_factor,
                                    "expected_move_pct": decision.expected_move_pct,
                                    "decision_cost_pct": decision.round_trip_cost_pct,
                                    "expected_horizon_seconds": decision.expected_horizon_seconds,
                                    "entry_bid": quote["bid"],
                                    "entry_ask": quote["ask"],
                                    "features": dict(features),
                                    "labels": {},
                                }
                            )
                            last_sample[market] = now
                    label_due_samples(now)

                    completed_max = sum(str(max_horizon) in sample["labels"] for sample in samples)
                    buy_count = sum(sample["signal"] == "BUY" for sample in samples)
                    print(
                        json.dumps(
                            {
                                "elapsed": round(now - started, 1),
                                "markets": len(markets),
                                "warmed": warmed,
                                "candidates": len(selected),
                                "book_markets": len(current_deep),
                                "samples": len(samples),
                                "buy_samples": buy_count,
                                f"labeled_{max_horizon}s": completed_max,
                                "regime": regime,
                                "ws_errors": sum(errors.values()),
                                "orders_submitted": 0,
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                next_scan = now + 1.0
            time.sleep(0.05)
    finally:
        for stream in streams:
            stream.stop()
        if deep is not None:
            deep.stop()
        client.close()

        finished = time.monotonic()
        # Remove process-local monotonic timestamps from the persisted dataset.
        persisted_samples = []
        for sample in samples:
            row = dict(sample)
            row.pop("created_mono", None)
            persisted_samples.append(row)
        summary = summarize_samples(persisted_samples, horizons)
        output = {
            "schema_version": 1,
            "run": {
                "started_epoch_ms": started_epoch_ms,
                "seconds": finished - started,
                "safe_krw_markets": len(markets),
                "trade_messages": sum(stream.message_count for stream in streams),
                "orderbook_messages": deep.message_count if deep is not None else 0,
                "websocket_errors": dict(errors),
                "assumed_fee_each_side": fee,
                "top_candidate_count": top_count,
                "sample_every_seconds": sample_every,
                "horizons_seconds": horizons,
                "max_book_markets": max_book_markets,
                "dropped_new_samples_due_book_cap": dropped_new_samples,
                "orders_submitted": 0,
            },
            "summary": summary,
            "reason_counts": reason_counts(persisted_samples),
            "limitations": [
                "API Key를 사용하지 않아 계정별 실제 수수료 대신 assumed_fee를 사용함",
                "entry ask와 future bid를 사용해 spread와 수수료는 반영하지만 주문 크기별 depth slippage는 반영하지 않음",
                "실제 주문 전송/체결 지연과 queue position을 반영하지 않음",
                "짧은 한 구간의 결과는 수익성 또는 미래 성과를 입증하지 않음",
                "chronological holdout은 데이터 분리일 뿐 아직 학습된 ExpectedMove 모델의 진정한 OOS 검증이 아님",
            ],
            "samples": persisted_samples,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        json.dumps(
            {
                "output": str(args.output),
                "samples": len(samples),
                "buys": sum(sample["signal"] == "BUY" for sample in samples),
                "orders_submitted": 0,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
