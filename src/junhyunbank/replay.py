from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .config import StrategyConfig
from .market_stream import MarketStream
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

    Production strategy code is not modified. Recorder trade/orderbook records
    carry exchange timestamps, so replay only replaces local receive freshness.
    """

    def __init__(self, config: StrategyConfig, clock: ReplayClock) -> None:
        super().__init__(config)
        self._replay_clock = clock

    def on_trade(self, event: dict[str, Any]) -> bool:
        market = str(event.get("code") or "")
        accepted = super().on_trade(event)
        if accepted:
            with self._lock:
                # super() used real process monotonic time. Replace it with the
                # recorded logical receive time before any replay evaluation.
                self._last_trade_received[market] = self._replay_clock.monotonic()
        return accepted

    def on_orderbook(self, event: dict[str, Any]) -> bool:
        market = str(event.get("code") or "")
        accepted = super().on_orderbook(event)
        if accepted:
            with self._lock:
                book = self._books.get(market)
                if book is not None:
                    book.received_at = self._replay_clock.monotonic()
        return accepted

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


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


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
    recorded_ts = _positive_int(record.get("exchange_timestamp_ms"))
    if kind == "trade":
        event_ts = _positive_int(event.get("trade_timestamp"))
        if event_ts is None and recorded_ts is None:
            # Older Upbit trade payloads can have only ``timestamp``. It is an
            # exchange-provided timestamp and is safe as a final compatibility
            # fallback; ambient replay wall time is never substituted.
            event_ts = _positive_int(event.get("timestamp"))
        if event_ts is not None and recorded_ts is not None and event_ts != recorded_ts:
            raise ReplayInputError(
                f"seq={record.get('seq')} trade exchange timestamp가 서로 다릅니다."
            )
        exchange_ts = event_ts or recorded_ts
        if exchange_ts is None:
            raise ReplayInputError(
                f"seq={record.get('seq')} trade에 exchange timestamp가 없습니다."
            )
        event["trade_timestamp"] = exchange_ts
    else:
        event_ts = _positive_int(event.get("timestamp"))
        if event_ts is not None and recorded_ts is not None and event_ts != recorded_ts:
            raise ReplayInputError(
                f"seq={record.get('seq')} orderbook exchange timestamp가 서로 다릅니다."
            )
        exchange_ts = event_ts or recorded_ts
        if exchange_ts is None:
            raise ReplayInputError(
                f"seq={record.get('seq')} orderbook에 exchange timestamp가 없습니다."
            )
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
        else f"시장 국면 {regime} · 신규 매수 차단"
    )
    book = strategy.book(market)
    entry_bid = book.bid_prices[0] if book and book.bid_prices else None
    entry_ask = book.ask_prices[0] if book and book.ask_prices else None
    row = {
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
    return _json_safe(row)


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
    control_reasons: Counter[str] = Counter()
    last_evaluation: dict[str, float] = {}
    last_control_evaluation: dict[str, float] = {}
    decisions: list[dict[str, Any]] = []
    control_decisions: list[dict[str, Any]] = []
    fingerprint = hashlib.sha256()
    control_fingerprint = hashlib.sha256()
    session_starts = 0
    session_ends = 0
    schema_versions: set[int] = set()
    nonzero_orders_meta = 0
    seq_regressions = 0
    seq_gaps = 0
    missing_seq_records = 0
    missing_received_ns_records = 0
    missing_received_monotonic_ns_records = 0
    unreplayable_clock_records = 0
    previous_seq: int | None = None
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    session_phase = "before"
    session_order_violations = 0
    records_before_session_start = 0
    records_after_session_end = 0
    unknown_market_events = 0
    subscription_violations = 0
    orderbook_subscription: set[str] | None = None
    hot_subscription: set[str] | None = None
    control_subscription: set[str] = set()
    role_aware_subscription_metadata = 0
    legacy_subscription_metadata = 0
    subscription_role_violations = 0
    implicit_legacy_orderbook_events = 0
    declared_orderbook_controls: int | None = None
    role_metadata_required = False
    session_metadata_violations = 0
    recorder_dropped = 0
    recorder_error_messages = 0
    websocket_error_events = 0
    websocket_errors_reported = 0
    stale_exchange_timestamp_events = 0
    future_exchange_timestamp_events = 0
    trade_events = 0
    orderbook_events = 0
    accepted_trade_events = 0
    accepted_orderbook_events = 0

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
        if seq is None:
            missing_seq_records += 1
        elif previous_seq is None:
            seq_gaps += max(0, seq - 1)
            previous_seq = seq
        elif seq <= previous_seq:
            seq_regressions += 1
        else:
            seq_gaps += max(0, seq - previous_seq - 1)
            previous_seq = seq

        received_ns = _positive_int(record.get("received_ns"))
        received_monotonic_ns = _positive_int(record.get("received_monotonic_ns"))
        if received_ns is None:
            missing_received_ns_records += 1
        if received_monotonic_ns is None:
            missing_received_monotonic_ns_records += 1
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
                    if session_phase != "before":
                        session_order_violations += 1
                    else:
                        session_phase = "active"
                    if "orderbook_controls" in data:
                        declared_controls = _finite_float(
                            data.get("orderbook_controls")
                        )
                        if (
                            declared_controls is None
                            or declared_controls < 0
                            or not declared_controls.is_integer()
                        ):
                            session_metadata_violations += 1
                            role_metadata_required = True
                        else:
                            declared_orderbook_controls = int(declared_controls)
                            role_metadata_required = declared_orderbook_controls > 0
                elif event_name == "session_end":
                    session_ends += 1
                    if session_phase != "active":
                        session_order_violations += 1
                    session_phase = "ended"
                    recorder_stats = data.get("recorder_stats")
                    if isinstance(recorder_stats, dict):
                        dropped = _finite_float(recorder_stats.get("dropped"))
                        if dropped is not None and dropped > 0:
                            recorder_dropped = max(recorder_dropped, int(dropped))
                        if str(recorder_stats.get("last_error") or "").strip():
                            recorder_error_messages += 1
                    reported = data.get("websocket_errors")
                    if isinstance(reported, dict):
                        for value in reported.values():
                            count = _finite_float(value)
                            if count is not None and count > 0:
                                websocket_errors_reported += int(count)
                orders = _finite_float(data.get("orders_submitted"))
                if orders is not None and orders != 0:
                    nonzero_orders_meta += 1
                if event_name in {"session_start", "session_end"}:
                    continue

                if session_phase == "before":
                    records_before_session_start += 1
                    continue
                if session_phase == "ended":
                    records_after_session_end += 1
                    continue

                if event_name == "websocket_error":
                    websocket_error_events += 1
                elif event_name == "orderbook_subscription":
                    raw_markets = data.get("markets")
                    if not isinstance(raw_markets, list):
                        subscription_violations += 1
                        changed = set(orderbook_subscription or ())
                        for changed_market in changed:
                            strategy.reset_orderbook(changed_market)
                            last_evaluation.pop(changed_market, None)
                            last_control_evaluation.pop(changed_market, None)
                        # A malformed update must not leave the previous role
                        # map active: that could classify later control books
                        # as production candidates.
                        orderbook_subscription = set()
                        hot_subscription = set()
                        control_subscription = set()
                        continue
                    desired = {
                        str(item) for item in raw_markets if str(item)
                    }
                    unknown = desired.difference(market_list)
                    if unknown:
                        subscription_violations += len(unknown)
                    desired.intersection_update(market_list)

                    has_hot_roles = "hot_markets" in data
                    has_control_roles = "control_markets" in data
                    role_metadata_valid = True
                    if not has_hot_roles and not has_control_roles:
                        # Recordings predating rotating controls subscribed only
                        # production candidates. Preserve their fingerprints by
                        # treating the legacy ``markets`` list as all-hot.
                        if role_metadata_required:
                            # A session that explicitly promised controls must
                            # carry the partition on every subscription update.
                            # Otherwise control books could be mislabeled hot.
                            role_metadata_valid = False
                            next_hot = set()
                            next_controls = set()
                        else:
                            legacy_subscription_metadata += 1
                            next_hot = set(desired)
                            next_controls = set()
                    elif has_hot_roles and has_control_roles:
                        role_aware_subscription_metadata += 1
                        raw_hot = data.get("hot_markets")
                        raw_controls = data.get("control_markets")
                        if not isinstance(raw_hot, list) or not isinstance(
                            raw_controls, list
                        ):
                            role_metadata_valid = False
                            next_hot = set()
                            next_controls = set()
                        else:
                            next_hot = {
                                str(item) for item in raw_hot if str(item)
                            }
                            next_controls = {
                                str(item) for item in raw_controls if str(item)
                            }
                            role_metadata_valid = (
                                not next_hot.intersection(next_controls)
                                and not next_hot.difference(market_list)
                                and not next_controls.difference(market_list)
                                and next_hot.union(next_controls) == desired
                            )
                    else:
                        # A partially written role map is ambiguous. Never
                        # guess which subscribed markets were trade candidates.
                        role_metadata_valid = False
                        next_hot = set()
                        next_controls = set()

                    if not role_metadata_valid:
                        subscription_role_violations += 1
                        desired = set()
                        next_hot = set()
                        next_controls = set()

                    previous_desired = set(orderbook_subscription or ())
                    previous_hot = set(hot_subscription or ())
                    previous_controls = set(control_subscription)
                    # Reset book continuity not only when the WebSocket union
                    # changes, but also when a market switches hot/control
                    # roles. A control book must not pre-warm a later production
                    # candidate in replay.
                    changed = (
                        desired.symmetric_difference(previous_desired)
                        | next_hot.symmetric_difference(previous_hot)
                        | next_controls.symmetric_difference(previous_controls)
                    )
                    for changed_market in changed:
                        strategy.reset_orderbook(changed_market)
                        last_evaluation.pop(changed_market, None)
                        last_control_evaluation.pop(changed_market, None)
                    orderbook_subscription = desired
                    hot_subscription = next_hot
                    control_subscription = next_controls
            continue

        if session_phase == "before":
            records_before_session_start += 1
            continue
        if session_phase == "ended":
            records_after_session_end += 1
            continue

        if kind == "trade":
            trade_events += 1
        elif kind == "orderbook":
            orderbook_events += 1
        normalized = strategy_event(record)
        if normalized is None:
            counts["ignored"] += 1
            continue
        event_kind, event = normalized
        if received_ns is None or received_monotonic_ns is None:
            # Freshness-dependent decisions cannot be reproduced without both
            # clocks written by the recorder. Keep the session diagnosable but
            # do not fabricate a decision on a frozen/fallback clock.
            unreplayable_clock_records += 1
            continue
        exchange_timestamp_ms = _positive_int(
            event.get("trade_timestamp")
            if event_kind == "trade"
            else event.get("timestamp")
        )
        if exchange_timestamp_ms is None:
            # strategy_event() normally raises first; this remains fail-closed
            # if its compatibility behavior ever changes.
            counts["invalid_exchange_timestamp"] += 1
            continue
        lag_seconds = received_ns / 1_000_000_000.0 - exchange_timestamp_ms / 1000.0
        if lag_seconds > MarketStream.DEFAULT_MAX_EVENT_LAG_SECONDS:
            stale_exchange_timestamp_events += 1
            continue
        if lag_seconds < -MarketStream.DEFAULT_MAX_EVENT_FUTURE_SECONDS:
            future_exchange_timestamp_events += 1
            continue
        market = str(event.get("code") or "")
        if market and market not in market_list:
            unknown_market_events += 1
            continue

        if event_kind == "trade":
            if not strategy.on_trade(event):
                counts["rejected_trade"] += 1
            else:
                accepted_trade_events += 1
            continue

        if (
            orderbook_subscription is not None
            and market not in orderbook_subscription
        ):
            subscription_violations += 1
            continue
        if orderbook_subscription is None and role_metadata_required:
            # Current recorder sessions declare rotating controls up front and
            # always emit roles before opening the orderbook stream. Without
            # that map, the first book's production/control role is unknowable.
            subscription_role_violations += 1
            continue
        if not strategy.on_orderbook(event):
            counts["rejected_orderbook"] += 1
            continue
        accepted_orderbook_events += 1
        if not market:
            continue
        if orderbook_subscription is None:
            # The earliest recorder format had no subscription metadata and no
            # controls. Keep it replayable as an implicit all-hot recording.
            is_control = False
            implicit_legacy_orderbook_events += 1
        elif market in control_subscription:
            is_control = True
        elif hot_subscription is not None and market in hot_subscription:
            is_control = False
        else:
            # Strict role metadata promises a complete partition of ``markets``.
            # Reaching here means it was internally inconsistent or corrupted.
            subscription_role_violations += 1
            continue
        now = clock.monotonic()
        evaluation_clock = (
            last_control_evaluation if is_control else last_evaluation
        )
        previous = evaluation_clock.get(market)
        if previous is not None and now - previous < opts.evaluate_every_seconds:
            continue
        evaluation_clock[market] = now
        row = _decision_row(
            strategy,
            market,
            clock=clock,
            markets=market_list,
            options=opts,
            received_ns=received_ns,
            seq=seq,
        )
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if is_control:
            control_decisions.append(row)
            control_reasons[str(row.get("reason") or "-")] += 1
            control_fingerprint.update(encoded)
            control_fingerprint.update(b"\n")
        else:
            decisions.append(row)
            reasons[str(row.get("reason") or "-")] += 1
            fingerprint.update(encoded)
            fingerprint.update(b"\n")

    complete_session = (
        session_starts == 1
        and session_ends == 1
        and session_order_violations == 0
        and records_before_session_start == 0
        and records_after_session_end == 0
    )
    market_events = trade_events + orderbook_events
    accepted_market_events = accepted_trade_events + accepted_orderbook_events
    evaluations = len(decisions)
    control_evaluations = len(control_decisions)
    integrity_counts = {
        "market_events": market_events,
        "trade_events": trade_events,
        "orderbook_events": orderbook_events,
        "accepted_market_events": accepted_market_events,
        "accepted_trade_events": accepted_trade_events,
        "accepted_orderbook_events": accepted_orderbook_events,
        "evaluations": evaluations,
        "control_evaluations": control_evaluations,
    }
    integrity_reasons: list[str] = []
    if not complete_session:
        integrity_reasons.append("incomplete_session")
    if seq_gaps:
        integrity_reasons.append("sequence_gaps")
    if seq_regressions:
        integrity_reasons.append("sequence_regressions")
    if missing_seq_records:
        integrity_reasons.append("missing_sequence")
    if missing_received_ns_records:
        integrity_reasons.append("missing_received_ns")
    if missing_received_monotonic_ns_records:
        integrity_reasons.append("missing_received_monotonic_ns")
    if unreplayable_clock_records:
        integrity_reasons.append("unreplayable_clocks")
    if market_events == 0:
        integrity_reasons.append("zero_market_events")
    if evaluations == 0:
        integrity_reasons.append("zero_evaluations")
    if unknown_market_events:
        integrity_reasons.append("unknown_market_events")
    if subscription_violations:
        integrity_reasons.append("subscription_violations")
    if subscription_role_violations:
        integrity_reasons.append("subscription_role_violations")
    if session_metadata_violations:
        integrity_reasons.append("session_metadata_violations")
    if recorder_dropped:
        integrity_reasons.append("recorder_drops")
    if recorder_error_messages:
        integrity_reasons.append("recorder_errors")
    if websocket_error_events:
        integrity_reasons.append("websocket_error_events")
    if websocket_errors_reported:
        integrity_reasons.append("reported_websocket_errors")
    if stale_exchange_timestamp_events:
        integrity_reasons.append("stale_exchange_timestamps")
    if future_exchange_timestamp_events:
        integrity_reasons.append("future_exchange_timestamps")
    if nonzero_orders_meta:
        integrity_reasons.append("nonzero_order_metadata")
    if counts.get("invalid_record", 0):
        integrity_reasons.append("invalid_records")
    if counts.get("ignored", 0):
        integrity_reasons.append("ignored_records")
    if counts.get("invalid_exchange_timestamp", 0):
        integrity_reasons.append("invalid_exchange_timestamps")
    if counts.get("rejected_trade", 0):
        integrity_reasons.append("rejected_trade_events")
    if counts.get("rejected_orderbook", 0):
        integrity_reasons.append("rejected_orderbook_events")
    data_integrity_ok = not integrity_reasons
    buy_decisions = sum(row.get("signal") == "BUY" for row in decisions)
    raw_buy_decisions = sum(row.get("raw_signal") == "BUY" for row in decisions)
    control_buy_decisions = sum(
        row.get("signal") == "BUY" for row in control_decisions
    )
    control_raw_buy_decisions = sum(
        row.get("raw_signal") == "BUY" for row in control_decisions
    )
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
            "data_integrity_ok": data_integrity_ok,
            "integrity_reasons": integrity_reasons,
            "integrity_counts": integrity_counts,
            "market_events": market_events,
            "trade_events": trade_events,
            "orderbook_events": orderbook_events,
            "accepted_market_events": accepted_market_events,
            "accepted_trade_events": accepted_trade_events,
            "accepted_orderbook_events": accepted_orderbook_events,
            "evaluations": evaluations,
            "control_evaluations": control_evaluations,
            "first_received_ns": first_received_ns,
            "last_received_ns": last_received_ns,
            "seq_gaps": seq_gaps,
            "seq_regressions": seq_regressions,
            "missing_seq_records": missing_seq_records,
            "missing_received_ns_records": missing_received_ns_records,
            "missing_received_monotonic_ns_records": missing_received_monotonic_ns_records,
            "unreplayable_clock_records": unreplayable_clock_records,
            "clock_retrograde_events": clock.retrograde_events,
            "nonzero_orders_meta": nonzero_orders_meta,
            "session_order_violations": session_order_violations,
            "records_before_session_start": records_before_session_start,
            "records_after_session_end": records_after_session_end,
            "unknown_market_events": unknown_market_events,
            "subscription_violations": subscription_violations,
            "subscription_role_violations": subscription_role_violations,
            "session_metadata_violations": session_metadata_violations,
            "declared_orderbook_controls": declared_orderbook_controls,
            "role_metadata_required": role_metadata_required,
            "role_aware_subscription_metadata": role_aware_subscription_metadata,
            "legacy_subscription_metadata": legacy_subscription_metadata,
            "implicit_legacy_orderbook_events": implicit_legacy_orderbook_events,
            "recorder_dropped": recorder_dropped,
            "recorder_error_messages": recorder_error_messages,
            "websocket_error_events": websocket_error_events,
            "websocket_errors_reported": websocket_errors_reported,
            "stale_exchange_timestamp_events": stale_exchange_timestamp_events,
            "future_exchange_timestamp_events": future_exchange_timestamp_events,
            "rejected_trade_events": counts.get("rejected_trade", 0),
            "rejected_orderbook_events": counts.get("rejected_orderbook", 0),
        },
        "summary": {
            "records_by_kind": dict(sorted(counts.items())),
            "markets": len(market_list),
            "evaluations": evaluations,
            "buy_decisions": buy_decisions,
            "raw_buy_decisions": raw_buy_decisions,
            "reason_counts": dict(
                sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
            ),
            "decision_fingerprint_sha256": fingerprint.hexdigest(),
        },
        "control_diagnostics": {
            "markets": len(
                {str(row.get("market") or "") for row in control_decisions}
            ),
            "evaluations": control_evaluations,
            "buy_decisions": control_buy_decisions,
            "raw_buy_decisions": control_raw_buy_decisions,
            "reason_counts": dict(
                sorted(
                    control_reasons.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            "decision_fingerprint_sha256": control_fingerprint.hexdigest(),
        },
        "decisions": decisions,
        "control_decisions": control_decisions,
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
