from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import __version__
from .config import StrategyConfig
from .recorder import iter_records, recording_files
from .strategy import MicroFlowStrategy


class ReplayInputError(ValueError):
    """Raised when a recording cannot be replayed safely/deterministically."""


@dataclass(slots=True)
class ReplayClock:
    """Logical clock driven only by recorder receive timestamps.

    The first valid recorded monotonic timestamp becomes replay time zero. Small
    backwards moves can exist because several WebSocket callback threads enqueue
    concurrently; those are clamped instead of letting freshness ages become
    negative. A true session restart must be selected as a separate session.
    """

    wall_seconds: float = 0.0
    monotonic_seconds: float = 0.0
    retrograde_events: int = 0
    _origin_wall_ns: int | None = None
    _origin_monotonic_ns: int | None = None

    def time(self) -> float:
        return self.wall_seconds

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def advance_record(self, record: dict[str, Any]) -> None:
        wall_ns = _positive_int(record.get("received_ns"))
        mono_ns = _positive_int(record.get("received_monotonic_ns"))

        if self._origin_wall_ns is None and wall_ns is not None:
            self._origin_wall_ns = wall_ns
        if self._origin_monotonic_ns is None and mono_ns is not None:
            self._origin_monotonic_ns = mono_ns

        if wall_ns is not None:
            self.wall_seconds = wall_ns / 1_000_000_000.0
        elif self._origin_wall_ns is not None:
            self.wall_seconds = (
                self._origin_wall_ns / 1_000_000_000.0
                + self.monotonic_seconds
            )

        candidate: float | None = None
        if mono_ns is not None and self._origin_monotonic_ns is not None:
            candidate = (mono_ns - self._origin_monotonic_ns) / 1_000_000_000.0
        elif wall_ns is not None and self._origin_wall_ns is not None:
            candidate = (wall_ns - self._origin_wall_ns) / 1_000_000_000.0

        if candidate is None or not math.isfinite(candidate):
            return
        candidate = max(0.0, candidate)
        if candidate < self.monotonic_seconds:
            self.retrograde_events += 1
            candidate = self.monotonic_seconds
        self.monotonic_seconds = candidate


class ReplayMicroFlowStrategy(MicroFlowStrategy):
    """MicroFlowStrategy with freshness ages bound to ReplayClock.

    The production strategy is intentionally left unchanged. Recorder trade and
    orderbook records always contain exchange timestamps, so only local receive
    freshness needs to be overridden for deterministic replay.
    """

    def __init__(self, config: StrategyConfig, clock: ReplayClock) -> None:
        super().__init__(config)
        self._replay_clock = clock

    def on_trade(self, event: dict[str, Any]) -> None:
        market = str(event.get("code") or "")
        before = self.latest_price(market) if market else None
        super().on_trade(event)
        after = self.latest_price(market) if market else None
        if market and (after is not None) and (after != before or market in self._current):
            with self._lock:
                self._last_trade_received[market] = self._replay_clock.monotonic()

    def on_orderbook(self, event: dict[str, Any]) -> None:
        market = str(event.get("code") or "")
        super().on_orderbook(event)
        if market:
            with self._lock:
                book = self._books.get(market)
                if book is not None:
                    book.received_at = self._replay_clock.monotonic()

    def book_age(self, market: str) -> float:
        book = self._books.get(market)
        if book is None:
            return float("inf")
        return max(0.0, self._replay_clock.monotonic() - book.received_at)

    def trade_age(self, market: str) -> float:
        received = self._last_trade_received.get(market)
        if received is None:
            return float("inf")
        return max(0.0, self._replay_clock.monotonic() - received)


@dataclass(slots=True)
class ReplayOptions:
    evaluate_every_seconds: float = 1.0
    bid_fee: float = 0.0005
    ask_fee: float = 0.0005

    def normalized(self) -> "ReplayOptions":
        interval = max(0.05, float(self.evaluate_every_seconds))
        bid = float(self.bid_fee)
        ask = float(self.ask_fee)
        if not (0.0 <= bid < 0.05 and 0.0 <= ask < 0.05):
            raise ReplayInputError("replay fee는 각 방향 0 이상 0.05 미만이어야 합니다.")
        return ReplayOptions(interval, bid, ask)


_SESSION_RE = re.compile(
    r"^(?P<session>.+)-(?P<segment>\d{6})\.jsonl(?:\.gz)?$"
)


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def recording_sessions(
    directory: Path,
    *,
    prefix: str = "upbit-public",
) -> dict[str, list[Path]]:
    """Group finalized recorder files by filename session id."""
    grouped: dict[str, list[Path]] = {}
    for path in recording_files(Path(directory), prefix=prefix):
        match = _SESSION_RE.match(path.name)
        if match is None:
            continue
        session = match.group("session")
        grouped.setdefault(session, []).append(path)
    for paths in grouped.values():
        paths.sort(key=lambda item: item.name)
    return dict(sorted(grouped.items()))


def select_recording_session(
    source: Path,
    *,
    prefix: str = "upbit-public",
    session: str = "latest",
) -> tuple[str, list[Path]]:
    """Select exactly one recorder session from a file or directory."""
    source = Path(source).expanduser()
    if source.is_file():
        match = _SESSION_RE.match(source.name)
        return (match.group("session") if match else source.stem), [source]
    if not source.is_dir():
        raise ReplayInputError(f"recording input을 찾을 수 없습니다: {source}")

    grouped = recording_sessions(source, prefix=prefix)
    if not grouped:
        raise ReplayInputError(
            f"{source}에 finalized {prefix}-*.jsonl(.gz) recording이 없습니다."
        )
    if session == "latest":
        key = sorted(grouped)[-1]
        return key, grouped[key]
    if session in grouped:
        return session, grouped[session]

    suffix_matches = [key for key in grouped if key.endswith(session)]
    if len(suffix_matches) == 1:
        key = suffix_matches[0]
        return key, grouped[key]
    raise ReplayInputError(
        f"session '{session}'을 찾지 못했습니다. 사용 가능: {', '.join(grouped)}"
    )


def session_metadata(files: Iterable[Path]) -> dict[str, Any]:
    """Return the single session_start metadata block for replay setup."""
    starts: list[dict[str, Any]] = []
    for record in iter_records(list(files)):
        if str(record.get("kind") or "") != "meta":
            continue
        data = record.get("data")
        if isinstance(data, dict) and data.get("event") == "session_start":
            starts.append(data)
            if len(starts) > 1:
                break
    if not starts:
        raise ReplayInputError("recording에 session_start metadata가 없습니다.")
    if len(starts) != 1:
        raise ReplayInputError(
            "한 replay 입력에 여러 session_start가 있습니다. session을 하나만 선택하세요."
        )
    return starts[0]


def strategy_config_from_metadata(metadata: dict[str, Any]) -> StrategyConfig:
    raw = metadata.get("strategy_config")
    if not isinstance(raw, dict):
        return StrategyConfig()
    allowed = {item.name for item in fields(StrategyConfig)}
    kwargs = {key: value for key, value in raw.items() if key in allowed}
    try:
        return StrategyConfig(**kwargs)
    except TypeError as exc:
        raise ReplayInputError(f"recorded StrategyConfig를 읽을 수 없습니다: {exc}") from exc


def strategy_event(record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    kind = str(record.get("kind") or "")
    if kind not in {"trade", "orderbook"}:
        return None
    market = str(record.get("market") or "")
    data = record.get("data")
    if not market or not isinstance(data, dict):
        return None
    event = dict(data)
    event["code"] = market
    exchange_ts = _positive_int(record.get("exchange_timestamp_ms"))
    if kind == "trade" and not event.get("trade_timestamp") and exchange_ts:
        event["trade_timestamp"] = exchange_ts
    if kind == "orderbook" and not event.get("timestamp") and exchange_ts:
        event["timestamp"] = exchange_ts
    return kind, event


def _decision_row(
    strategy: ReplayMicroFlowStrategy,
    market: str,
    *,
    clock: ReplayClock,
    markets: list[str],
    options: ReplayOptions,
    received_ns: int | None,
    seq: int | None,
) -> dict[str, Any]:
    regime, regime_factor = strategy.market_regime(markets)
    features = strategy._feature_set(market)
    decision = strategy.evaluate_entry(
        market,
        bid_fee=options.bid_fee,
        ask_fee=options.ask_fee,
        health=1.0,
        regime_factor=regime_factor,
    )
    effective_signal = decision.signal.value if regime_factor > 0 else "HOLD"
    reason = (
        decision.reason
        if regime_factor > 0
        else "시장 PANIC · 신규 매수 차단"
    )
    book = strategy.book(market)
    entry_bid = book.bid_prices[0] if book and book.bid_prices else None
    entry_ask = book.ask_prices[0] if book and book.ask_prices else None
    return {
        "seq": seq,
        "received_ns": received_ns,
        "replay_monotonic_seconds": clock.monotonic(),
        "market": market,
        "signal": effective_signal,
        "raw_signal": decision.signal.value,
        "kind": decision.kind.value,
        "score": decision.score,
        "reason": reason,
        "regime": regime,
        "regime_factor": regime_factor,
        "expected_move_pct": decision.expected_move_pct,
        "decision_cost_pct": decision.round_trip_cost_pct,
        "capital_fraction": decision.capital_fraction,
        "initial_risk_pct": decision.initial_risk_pct,
        "expected_horizon_seconds": decision.expected_horizon_seconds,
        "trade_age_seconds": strategy.trade_age(market),
        "book_age_seconds": strategy.book_age(market),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "features": dict(features) if features else None,
    }


def replay_records(
    records: Iterable[dict[str, Any]],
    *,
    strategy_config: StrategyConfig,
    markets: Iterable[str],
    options: ReplayOptions | None = None,
) -> dict[str, Any]:
    """Replay one recorder session in file order without sleeping or orders."""
    opts = (options or ReplayOptions()).normalized()
    market_list = sorted({str(item) for item in markets if str(item)})
    clock = ReplayClock()
    strategy = ReplayMicroFlowStrategy(strategy_config, clock)
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    last_evaluation: dict[str, float] = {}
    decisions: list[dict[str, Any]] = []
    fingerprint = hashlib.sha256()
    session_starts = 0
    session_ends = 0
    schema_versions: set[int] = set()
    nonzero_orders_meta = 0
    seq_regressions = 0
    previous_seq: int | None = None
    first_received_ns: int | None = None
    last_received_ns: int | None = None

    for record in records:
        if not isinstance(record, dict):
            counts["invalid_record"] += 1
            continue
        schema = _positive_int(record.get("schema_version"))
        if schema is None:
            raise ReplayInputError("schema_version이 없는 recorder record가 있습니다.")
        schema_versions.add(schema)
        if schema != 1:
            raise ReplayInputError(f"지원하지 않는 recorder schema_version={schema}")

        seq = _positive_int(record.get("seq"))
        if seq is not None and previous_seq is not None and seq <= previous_seq:
            seq_regressions += 1
        if seq is not None:
            previous_seq = seq

        received_ns = _positive_int(record.get("received_ns"))
        if received_ns is not None:
            if first_received_ns is None:
                first_received_ns = received_ns
            last_received_ns = received_ns
        clock.advance_record(record)

        kind = str(record.get("kind") or "")
        counts[kind or "unknown"] += 1
        if kind == "meta":
            data = record.get("data")
            if isinstance(data, dict):
                event_name = str(data.get("event") or "")
                if event_name == "session_start":
                    session_starts += 1
                elif event_name == "session_end":
                    session_ends += 1
                orders = _finite_float(data.get("orders_submitted"))
                if orders is not None and orders != 0:
                    nonzero_orders_meta += 1
            continue

        normalized = strategy_event(record)
        if normalized is None:
            counts["ignored"] += 1
            continue
        event_kind, event = normalized
        market = str(event.get("code") or "")
        if market and market not in market_list:
            market_list.append(market)
            market_list.sort()

        if event_kind == "trade":
            strategy.on_trade(event)
            continue

        strategy.on_orderbook(event)
        if not market:
            continue
        now = clock.monotonic()
        previous = last_evaluation.get(market)
        if previous is not None and now - previous < opts.evaluate_every_seconds:
            continue
        last_evaluation[market] = now
        row = _decision_row(
            strategy,
            market,
            clock=clock,
            markets=market_list,
            options=opts,
            received_ns=received_ns,
            seq=seq,
        )
        decisions.append(row)
        reasons[str(row.get("reason") or "-")] += 1
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        fingerprint.update(encoded)
        fingerprint.update(b"\n")

    complete_session = session_starts == 1 and session_ends == 1
    buy_decisions = sum(row.get("signal") == "BUY" for row in decisions)
    raw_buy_decisions = sum(row.get("raw_signal") == "BUY" for row in decisions)
    return {
        "schema_version": 1,
        "replay": {
            "junhyunbank_version": __version__,
            "strategy_config": asdict(strategy_config),
            "evaluate_every_seconds": opts.evaluate_every_seconds,
            "bid_fee": opts.bid_fee,
            "ask_fee": opts.ask_fee,
            "orders_submitted": 0,
        },
        "input": {
            "recorder_schema_versions": sorted(schema_versions),
            "session_start_count": session_starts,
            "session_end_count": session_ends,
            "complete_session": complete_session,
            "first_received_ns": first_received_ns,
            "last_received_ns": last_received_ns,
            "seq_regressions": seq_regressions,
            "clock_retrograde_events": clock.retrograde_events,
            "nonzero_orders_meta": nonzero_orders_meta,
        },
        "summary": {
            "records_by_kind": dict(sorted(counts.items())),
            "markets": len(market_list),
            "evaluations": len(decisions),
            "buy_decisions": buy_decisions,
            "raw_buy_decisions": raw_buy_decisions,
            "reason_counts": dict(
                sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
            ),
            "decision_fingerprint_sha256": fingerprint.hexdigest(),
        },
        "decisions": decisions,
    }


def replay_files(
    files: Iterable[Path],
    *,
    options: ReplayOptions | None = None,
) -> dict[str, Any]:
    """Replay finalized files using their recorded market/config metadata."""
    selected = [Path(path) for path in files]
    if not selected:
        raise ReplayInputError("replay할 recording 파일이 없습니다.")
    metadata = session_metadata(selected)
    orders = _finite_float(metadata.get("orders_submitted"))
    if orders is not None and orders != 0:
        raise ReplayInputError("Public-only recording이 아닌 입력은 replay하지 않습니다.")
    markets = metadata.get("safe_krw_markets")
    if not isinstance(markets, list) or not markets:
        raise ReplayInputError("session_start에 safe_krw_markets가 없습니다.")
    config = strategy_config_from_metadata(metadata)
    result = replay_records(
        iter_records(selected),
        strategy_config=config,
        markets=markets,
        options=options,
    )
    result["source"] = {
        "recorded_junhyunbank_version": metadata.get("junhyunbank_version"),
        "files": [path.name for path in selected],
    }
    return result
