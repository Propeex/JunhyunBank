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
from dataclasses import asdict
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from junhyunbank import __version__
from junhyunbank.config import SafetyConfig, StrategyConfig
from junhyunbank.market_stream import MarketStream
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.strategy import MicroFlowStrategy
from junhyunbank.upbit import UpbitClient
from junhyunbank.validation import (
    absolute_mid_move,
    count_role_buy_samples,
    net_round_trip_return,
    reason_counts,
    rotating_control_markets,
    sampling_role_entries,
    summarize_samples,
    validation_integrity_report,
)


def _parse_horizons(value: str) -> list[int]:
    result = sorted(
        {int(part.strip()) for part in value.split(",") if part.strip()}
    )
    if not result or any(item <= 0 for item in result):
        raise argparse.ArgumentTypeError(
            "horizons는 양의 초 단위 정수 목록이어야 합니다."
        )
    return result


def _safe_quote(event: dict[str, Any]) -> tuple[float, float] | None:
    units = event.get("orderbook_units") or []
    if (
        not isinstance(units, list)
        or not units
        or not isinstance(units[0], dict)
    ):
        return None
    try:
        bid = float(units[0].get("bid_price") or 0.0)
        ask = float(units[0].get("ask_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if (
        not (math.isfinite(bid) and math.isfinite(ask))
        or bid <= 0
        or ask <= 0
        or bid > ask
    ):
        return None
    return bid, ask


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=900.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--horizons",
        type=_parse_horizons,
        default=_parse_horizons("30,60,120,300"),
    )
    parser.add_argument("--sample-every", type=float, default=10.0)
    parser.add_argument("--top", type=int, default=24)
    parser.add_argument(
        "--control-count",
        type=int,
        default=4,
        help="Hot 후보 밖에서 outcome-independent하게 순환 표본화할 시장 수",
    )
    parser.add_argument("--control-rotate-seconds", type=float, default=60.0)
    parser.add_argument("--assumed-fee", type=float, default=0.0005)
    parser.add_argument("--max-book-markets", type=int, default=100)
    parser.add_argument("--max-label-delay", type=float, default=3.0)
    args = parser.parse_args()

    seconds = max(30.0, float(args.seconds))
    horizons = list(args.horizons)
    max_horizon = max(horizons)
    sample_every = max(1.0, float(args.sample_every))
    top_count = max(1, min(50, int(args.top)))
    control_count = max(0, min(100 - top_count, int(args.control_count)))
    control_rotate_seconds = max(10.0, float(args.control_rotate_seconds))
    max_book_markets = min(
        100,
        max(top_count + control_count, int(args.max_book_markets)),
    )
    max_label_delay = max(0.25, min(30.0, float(args.max_label_delay)))
    fee = float(args.assumed_fee)
    if not (0 <= fee < 0.05):
        raise SystemExit("--assumed-fee는 0 이상 0.05 미만이어야 합니다.")

    client = UpbitClient()
    strategy = MicroFlowStrategy(StrategyConfig())
    lock = threading.RLock()
    errors: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    quotes: dict[str, dict[str, float]] = {}
    samples: list[dict[str, Any]] = []
    last_sample: dict[str, float] = {}
    dropped_new_samples = 0
    peak_unresolved_without_book = 0
    retired_orderbook_messages = 0
    sample_sequence = 0

    def trade(event: dict[str, Any]) -> None:
        with lock:
            if not strategy.on_trade(event):
                quality_counts["rejected_trade_events"] += 1

    def book(event: dict[str, Any]) -> None:
        quote = _safe_quote(event)
        with lock:
            accepted = strategy.on_orderbook(event)
            if not accepted:
                quality_counts["rejected_orderbook_events"] += 1
            if quote is None:
                quality_counts["invalid_orderbook_quotes"] += 1
            if accepted and quote is not None:
                market = str(event.get("code") or "")
                if market:
                    quotes[market] = {
                        "bid": quote[0],
                        "ask": quote[1],
                        "received_mono": time.monotonic(),
                        "epoch_ms": float(event["timestamp"]),
                    }

    def error(message: str) -> None:
        with lock:
            errors[str(message)] += 1

    def trade_status(payload: dict[str, Any]) -> None:
        if str(payload.get("state") or "") != "reconnecting":
            return
        affected = [str(item) for item in payload.get("market_codes") or []]
        with lock:
            strategy.reset_trades(affected)

    def book_status(payload: dict[str, Any]) -> None:
        if str(payload.get("state") or "") != "reconnecting":
            return
        affected = [str(item) for item in payload.get("market_codes") or []]
        with lock:
            for market in affected:
                strategy.reset_orderbook(market)
                quotes.pop(market, None)

    try:
        rows = client.get_markets()
    except Exception:
        client.close()
        raise
    try:
        markets = TradingEngine.validated_initial_markets(rows, SafetyConfig())
    except RuntimeError as exc:
        client.close()
        raise SystemExit(str(exc)) from exc

    streams = [
        MarketStream(
            markets[index : index + 100],
            on_trade=trade,
            on_error=error,
            on_status=trade_status,
        )
        for index in range(0, len(markets), 100)
    ]
    deep: MarketStream | None = None
    current_deep: list[str] = []
    started = time.monotonic()
    started_epoch_ms = int(time.time() * 1000)
    sampling_deadline = started + max(
        0.0, seconds - max_horizon - max_label_delay
    )

    def unresolved_markets(now: float) -> list[str]:
        del now
        due_by_market: dict[str, float] = {}
        for sample in samples:
            labels = sample["labels"]
            for horizon in horizons:
                if str(horizon) in labels:
                    continue
                due = float(sample["created_mono"]) + horizon
                market = str(sample["market"])
                due_by_market[market] = min(
                    due_by_market.get(market, due), due
                )
        return [
            market
            for market, _ in sorted(
                due_by_market.items(), key=lambda item: (item[1], item[0])
            )
        ]

    def desired_book_markets(selected: list[str], now: float) -> list[str]:
        # Protect already-created samples first. A missing new sample is less
        # damaging than silently assigning an existing sample a late quote.
        result: list[str] = []
        for market in unresolved_markets(now) + selected:
            if market not in result:
                result.append(market)
            if len(result) >= max_book_markets:
                break
        return sorted(result)

    def label_due_samples(now: float) -> None:
        for sample in samples:
            market = str(sample["market"])
            quote = quotes.get(market)
            quote_mono = (
                float(quote["received_mono"]) if quote is not None else None
            )
            for horizon in horizons:
                key = str(horizon)
                if key in sample["labels"]:
                    continue
                due = float(sample["created_mono"]) + horizon
                if now < due:
                    continue
                if (
                    quote is not None
                    and quote_mono is not None
                    and due <= quote_mono <= due + max_label_delay
                ):
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
                    if net is not None and move is not None:
                        sample["labels"][key] = {
                            "status": "labeled",
                            "future_bid": quote["bid"],
                            "future_ask": quote["ask"],
                            "future_epoch_ms": int(quote["epoch_ms"]),
                            "label_delay_seconds": max(
                                0.0, quote_mono - due
                            ),
                            "net_return": net,
                            "absolute_mid_move": move,
                        }
                        continue
                if now >= due + max_label_delay:
                    sample["labels"][key] = {
                        "status": "missed",
                        "reason": "fresh orderbook quote unavailable inside label window",
                    }

    completed_cleanly = False
    final_integrity: dict[str, Any] = {
        "data_integrity_ok": False,
        "reasons": ["run_incomplete"],
        "counts": {},
    }
    final_errors: dict[str, int] = {}
    try:
        for stream in streams:
            stream.start()

        next_scan = 0.0
        next_control_rotation = 0.0
        control_cursor = 0
        control_markets: list[str] = []
        candidate_markets: list[str] = []
        candidate_entered_at: dict[str, float] = {}
        while time.monotonic() - started < seconds:
            now = time.monotonic()
            if now >= next_scan:
                with lock:
                    previous_candidates = set(candidate_markets)
                    previous_controls = set(control_markets)
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
                        deep_limit=top_count,
                    )
                    selected = selection.selected
                    candidate_entered_at = {
                        market: candidate_entered_at.get(market, now)
                        for market in selected
                    }
                    candidate_markets = list(selected)
                    retained_controls = [
                        market for market in control_markets if market not in selected
                    ]
                    if (
                        now >= next_control_rotation
                        or len(retained_controls) < control_count
                    ):
                        control_markets, control_cursor = rotating_control_markets(
                            markets,
                            excluded=selected,
                            count=control_count,
                            cursor=control_cursor,
                        )
                        next_control_rotation = now + control_rotate_seconds
                    else:
                        control_markets = retained_controls
                    sampled_markets = selected + control_markets
                    desired = desired_book_markets(sampled_markets, now)
                    role_entries = set(
                        sampling_role_entries(
                            previous_candidates=previous_candidates,
                            previous_controls=previous_controls,
                            next_candidates=selected,
                            next_controls=control_markets,
                        )
                    )
                    previous = set(current_deep)
                    added = set(desired) - previous
                    # A control or label-retained subscription becoming a
                    # production candidate is a new deep-analysis entry in
                    # live trading even when the WebSocket union is unchanged.
                    # Reset its book, quote and sampling timer accordingly.
                    for market in sorted(added | role_entries):
                        strategy.reset_orderbook(market)
                        quotes.pop(market, None)
                        last_sample.pop(market, None)
                    if desired != current_deep:
                        if deep is None and desired:
                            deep = MarketStream(
                                desired,
                                on_orderbook=book,
                                on_error=error,
                                on_status=book_status,
                                orderbook_depth=max(
                                    5, strategy.config.orderbook_depth
                                ),
                                name="edge-validator-orderbook",
                            )
                            deep.start()
                        elif deep is not None and desired:
                            deep.update_markets(desired)
                        elif deep is not None and not desired:
                            retired_orderbook_messages += deep.message_count
                            deep.stop()
                            deep = None
                        current_deep = desired

                    unresolved = unresolved_markets(now)
                    unresolved_without_book = sum(
                        market not in current_deep for market in unresolved
                    )
                    peak_unresolved_without_book = max(
                        peak_unresolved_without_book,
                        unresolved_without_book,
                    )

                    regime, regime_factor = strategy.market_regime(markets)
                    warmed = sum(
                        strategy.warmup_ratio(market) >= 1.0
                        for market in markets
                    )
                    if now <= sampling_deadline:
                        for market in sampled_markets:
                            if (
                                now - last_sample.get(market, 0.0)
                                < sample_every
                            ):
                                continue
                            if market not in current_deep:
                                dropped_new_samples += 1
                                continue
                            quote = quotes.get(market)
                            if (
                                not quote
                                or now - float(quote["received_mono"]) > 3.0
                            ):
                                continue
                            features = strategy._feature_set(market)
                            if not features:
                                continue

                            data_age = max(
                                strategy.trade_age(market),
                                strategy.book_age(market),
                            )
                            if data_age > 3.0:
                                signal = "HOLD"
                                raw_signal = "HOLD"
                                kind = "NONE"
                                score = float(features.get("quality") or 0.0) * 100.0
                                reason = "체결/호가 수신 대기 또는 3초 이상 지연"
                                expected_move = float(
                                    features.get("expected_move") or 0.0
                                )
                                decision_cost = (
                                    2.0 * fee
                                    + float(features.get("spread_pct") or 0.0)
                                    * 1.5
                                )
                                expected_horizon = 0.0
                            else:
                                decision = strategy.evaluate_entry(
                                    market,
                                    bid_fee=fee,
                                    ask_fee=fee,
                                    health=1.0,
                                    regime_factor=regime_factor,
                                )
                                raw_signal = decision.signal.value
                                signal = (
                                    raw_signal
                                    if regime_factor > 0
                                    else "HOLD"
                                )
                                kind = decision.kind.value
                                score = decision.score
                                reason = (
                                    decision.reason
                                    if regime_factor > 0
                                    else f"시장 국면 {regime} · 신규 매수 차단"
                                )
                                expected_move = (
                                    decision.expected_move_pct
                                    or float(
                                        features.get("expected_move") or 0.0
                                    )
                                )
                                decision_cost = (
                                    decision.round_trip_cost_pct
                                    or 2.0 * fee
                                    + float(features.get("spread_pct") or 0.0)
                                    * 1.5
                                )
                                expected_horizon = (
                                    decision.expected_horizon_seconds
                                )

                            created_epoch_ms = int(time.time() * 1000)
                            sample_sequence += 1
                            samples.append(
                                {
                                    "sample_id": (
                                        f"{created_epoch_ms}-{sample_sequence}-{market}"
                                    ),
                                    "created_mono": now,
                                    "created_epoch_ms": created_epoch_ms,
                                    "market": market,
                                    "sample_role": (
                                        "candidate" if market in selected else "control"
                                    ),
                                    "signal": signal,
                                    "raw_signal": raw_signal,
                                    "kind": kind,
                                    "score": score,
                                    "reason": reason,
                                    "regime": regime,
                                    "regime_factor": regime_factor,
                                    "data_age_seconds": data_age,
                                    "entry_quote_age_seconds": max(
                                        0.0,
                                        now - float(quote["received_mono"]),
                                    ),
                                    "expected_move_pct": expected_move,
                                    "decision_cost_pct": decision_cost,
                                    "expected_horizon_seconds": expected_horizon,
                                    "entry_bid": quote["bid"],
                                    "entry_ask": quote["ask"],
                                    "features": dict(features),
                                    "labels": {},
                                }
                            )
                            last_sample[market] = now
                    label_due_samples(now)

                    completed_max = sum(
                        str(max_horizon) in sample["labels"]
                        and sample["labels"][str(max_horizon)].get("status")
                        == "labeled"
                        for sample in samples
                    )
                    missed_max = sum(
                        str(max_horizon) in sample["labels"]
                        and sample["labels"][str(max_horizon)].get("status")
                        == "missed"
                        for sample in samples
                    )
                    buy_count = count_role_buy_samples(
                        samples, role="candidate"
                    )
                    control_buy_count = count_role_buy_samples(
                        samples, role="control"
                    )
                    print(
                        json.dumps(
                            {
                                "elapsed": round(now - started, 1),
                                "markets": len(markets),
                                "warmed": warmed,
                                "candidates": len(selected),
                                "controls": len(control_markets),
                                "book_markets": len(current_deep),
                                "samples": len(samples),
                                "buy_samples": buy_count,
                                "control_buy_samples": control_buy_count,
                                f"labeled_{max_horizon}s": completed_max,
                                f"missed_{max_horizon}s": missed_max,
                                "unresolved_without_book": unresolved_without_book,
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
        completed_cleanly = True
    finally:
        for stream in streams:
            stream.stop()
        if deep is not None:
            deep.stop()
        active_orderbook_messages = deep.message_count if deep is not None else 0
        client.close()

        finished = time.monotonic()
        with lock:
            # Freeze one last due-label pass after all producers are stopped.
            # A normal run reserves max_horizon + max_label_delay at its tail,
            # so any remaining pending/missed label is an integrity failure.
            label_due_samples(finished)
            persisted_samples = []
            for sample in samples:
                row = dict(sample)
                row.pop("created_mono", None)
                persisted_samples.append(row)
            final_errors = dict(errors)
            final_quality_counts = dict(quality_counts)
        final_integrity = validation_integrity_report(
            persisted_samples,
            horizons,
            completed_cleanly=completed_cleanly,
            websocket_errors=final_errors,
            rejected_trade_events=final_quality_counts.get(
                "rejected_trade_events", 0
            ),
            rejected_orderbook_events=final_quality_counts.get(
                "rejected_orderbook_events", 0
            ),
            invalid_orderbook_quotes=final_quality_counts.get(
                "invalid_orderbook_quotes", 0
            ),
            dropped_new_samples_due_book_cap=dropped_new_samples,
            controls_required=control_count > 0,
        )
        summary = summarize_samples(persisted_samples, horizons)
        output = {
            "schema_version": 3,
            "run": {
                "junhyunbank_version": __version__,
                "started_epoch_ms": started_epoch_ms,
                "seconds": finished - started,
                "safe_krw_markets": len(markets),
                "trade_messages": sum(
                    stream.message_count for stream in streams
                ),
                "orderbook_messages": (
                    retired_orderbook_messages + active_orderbook_messages
                ),
                "websocket_errors": final_errors,
                "completed_cleanly": completed_cleanly,
                "data_integrity_ok": final_integrity[
                    "data_integrity_ok"
                ],
                "integrity_reasons": final_integrity["reasons"],
                "integrity_counts": final_integrity["counts"],
                "assumed_fee_each_side": fee,
                "top_candidate_count": top_count,
                "control_market_count": control_count,
                "control_rotate_seconds": control_rotate_seconds,
                "sample_every_seconds": sample_every,
                "horizons_seconds": horizons,
                "max_book_markets": max_book_markets,
                "max_label_delay_seconds": max_label_delay,
                "dropped_new_samples_due_book_cap": dropped_new_samples,
                "peak_unresolved_markets_without_book": (
                    peak_unresolved_without_book
                ),
                "orders_submitted": 0,
                "strategy_config": asdict(strategy.config),
            },
            "summary": summary,
            "reason_counts": reason_counts(
                sample
                for sample in persisted_samples
                if sample.get("sample_role") != "control"
            ),
            "control_reason_counts": reason_counts(
                sample
                for sample in persisted_samples
                if sample.get("sample_role") == "control"
            ),
            "limitations": [
                "API Key를 사용하지 않아 계정별 실제 수수료 대신 assumed_fee를 사용함",
                "entry ask와 future bid를 사용해 spread와 수수료는 반영하지만 주문 크기별 depth slippage는 반영하지 않음",
                "실제 주문 전송/체결 지연과 queue position을 반영하지 않음",
                "BUY 표본은 전략 1차 진입판단이며 계정 잔고·pending 주문·최종 유동성/슬리피지 게이트를 통과한 실제 주문을 의미하지 않음",
                "control 표본은 현재 Hot 후보 밖에서 미래 결과를 보지 않고 순환 선택하지만 전체 시장의 완전한 무작위 표본은 아님",
                "짧은 한 구간의 결과는 수익성 또는 미래 성과를 입증하지 않음",
                "시간순 holdout은 forward-label overlap을 purge하지만 아직 학습된 ExpectedMove 모델의 진정한 OOS 검증은 아님",
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
                "buys": count_role_buy_samples(
                    samples, role="candidate"
                ),
                "control_buys": count_role_buy_samples(
                    samples, role="control"
                ),
                "data_integrity_ok": final_integrity[
                    "data_integrity_ok"
                ],
                "integrity_reasons": final_integrity["reasons"],
                "integrity_counts": final_integrity["counts"],
                "websocket_errors": sum(final_errors.values()),
                "orders_submitted": 0,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    return 0 if final_integrity["data_integrity_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
