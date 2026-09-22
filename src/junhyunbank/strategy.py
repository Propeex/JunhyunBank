from __future__ import annotations

import math
import statistics
import time
import threading
from functools import wraps
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Any

from .config import StrategyConfig
from .market_stream import TRANSPORT_AGE_SECONDS_KEY
from .models import Signal, SignalKind, StrategyDecision


@dataclass(slots=True)
class SecondFrame:
    second: int
    open: float
    high: float
    low: float
    close: float
    bid_value: float = 0.0
    ask_value: float = 0.0
    trade_value: float = 0.0
    trades: int = 0


@dataclass(slots=True)
class BookSnapshot:
    received_at: float
    bid_prices: list[float]
    bid_sizes: list[float]
    ask_prices: list[float]
    ask_sizes: list[float]
    imbalance: float
    micro_bias: float
    spread_pct: float


@dataclass(slots=True)
class CandidateSelection:
    selected: list[str]
    actionable_ranked: list[tuple[str, float]]
    stale_skipped: int
    supplemented: int


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _percentile_rank(history: list[float], value: float) -> float:
    if not history:
        return 0.5
    below = sum(1 for item in history if item < value)
    equal = sum(1 for item in history if item == value)
    return _clamp((below + 0.5 * equal) / len(history))


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = _clamp(q) * (len(ordered) - 1)
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return ordered[lo]
    weight = position - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _rolling_sums(values: list[float], window: int) -> list[float]:
    if window <= 0 or len(values) < window:
        return []
    result: list[float] = []
    running = sum(values[:window])
    result.append(running)
    for idx in range(window, len(values)):
        running += values[idx] - values[idx - window]
        result.append(running)
    return result


def _event_received_at(event: dict[str, Any]) -> float | None:
    """Map trusted stream transport age onto the local monotonic clock."""
    now = time.monotonic()
    if TRANSPORT_AGE_SECONDS_KEY not in event:
        return now
    raw_age = event.get(TRANSPORT_AGE_SECONDS_KEY)
    if isinstance(raw_age, bool):
        return None
    try:
        transport_age = float(raw_age)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(transport_age) or transport_age < 0.0:
        return None
    return now - transport_age


class MicroFlowStrategy:
    """상대적 수급/거래활동/호가/모멘텀을 결합하는 실시간 단타 전략."""

    def __init__(self, config: StrategyConfig) -> None:
        self._lock = threading.RLock()
        self.config = config
        maxlen = max(config.baseline_seconds + 120, 600)
        self._frames: dict[str, deque[SecondFrame]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )
        self._current: dict[str, SecondFrame] = {}
        self._books: dict[str, BookSnapshot] = {}
        self._book_history: dict[str, deque[tuple[int, float, float]]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )
        self._weak_evidence: dict[str, tuple[float, int, int, int, float]] = {}
        self._last_trade_received: dict[str, float] = {}
        self._last_trade_timestamp_ms: dict[str, int] = {}
        self._last_trade_sequence: dict[str, int] = {}
        self._last_trade_signature: dict[str, tuple[Any, ...]] = {}
        self._last_book_timestamp_ms: dict[str, int] = {}
        self._last_book_signature: dict[str, tuple[Any, ...]] = {}
        self._blocked_after_exit: set[str] = set()

    @synchronized
    def on_trade(self, event: dict[str, Any]) -> bool:
        market = str(event.get("code") or "")
        received_at = _event_received_at(event)
        if received_at is None:
            return False
        try:
            price = float(event.get("trade_price") or 0.0)
            volume = float(event.get("trade_volume") or 0.0)
            timestamp_ms = int(
                event.get("trade_timestamp")
                or event.get("timestamp")
                or time.time() * 1000
            )
        except (TypeError, ValueError, OverflowError):
            return False
        if not market or not math.isfinite(price) or not math.isfinite(volume) or price <= 0 or volume <= 0:
            return False
        if timestamp_ms <= 0:
            return False

        sequence: int | None = None
        if event.get("sequential_id") is not None:
            try:
                sequence = int(event["sequential_id"])
            except (TypeError, ValueError, OverflowError):
                return False
            previous_sequence = self._last_trade_sequence.get(market)
            if previous_sequence is not None and sequence <= previous_sequence:
                return False

        previous_timestamp = self._last_trade_timestamp_ms.get(market)
        if previous_timestamp is not None and timestamp_ms < previous_timestamp:
            return False

        second = timestamp_ms // 1000
        side = str(event.get("ask_bid") or "").upper()
        value = price * volume
        if not math.isfinite(value):
            return False
        signature = (timestamp_ms, sequence, price, volume, side)
        if self._last_trade_signature.get(market) == signature:
            return False
        frame = self._current.get(market)

        if frame is not None and second < frame.second:
            # 과거/역순 이벤트가 현재 시간 프레임을 훼손하지 않게 버린다.
            return False

        self._last_trade_received[market] = received_at
        self._last_trade_timestamp_ms[market] = timestamp_ms
        self._last_trade_signature[market] = signature
        if sequence is not None:
            self._last_trade_sequence[market] = sequence

        if frame is None:
            frame = SecondFrame(second, price, price, price, price)
            self._current[market] = frame
        elif second > frame.second:
            history = self._frames[market]
            history.append(frame)
            gap = second - frame.second
            if gap > self.config.baseline_seconds:
                # 너무 오래 거래가 없었던 종목은 오래된 통계를 재사용하지 않는다.
                history.clear()
                self._book_history[market].clear()
            else:
                # 체결이 없는 초도 실제 시간축의 일부다. 0거래/보합 프레임으로 채운다.
                previous_close = frame.close
                for missing_second in range(frame.second + 1, second):
                    history.append(
                        SecondFrame(
                            missing_second,
                            previous_close,
                            previous_close,
                            previous_close,
                            previous_close,
                        )
                    )
            frame = SecondFrame(second, price, price, price, price)
            self._current[market] = frame

        frame.high = max(frame.high, price)
        frame.low = min(frame.low, price)
        frame.close = price
        frame.trade_value += value
        frame.trades += 1
        if side == "BID":
            frame.bid_value += value
        elif side == "ASK":
            frame.ask_value += value
        return True

    @synchronized
    def on_orderbook(self, event: dict[str, Any]) -> bool:
        market = str(event.get("code") or "")
        received_at = _event_received_at(event)
        if received_at is None:
            return False
        units = event.get("orderbook_units") or []
        if not market or not isinstance(units, list) or not units:
            return False
        depth = max(1, min(self.config.orderbook_depth, len(units)))
        selected = units[:depth]
        if any(not isinstance(row, dict) for row in selected):
            return False
        try:
            bid_prices = [float(row.get("bid_price") or 0.0) for row in selected]
            ask_prices = [float(row.get("ask_price") or 0.0) for row in selected]
            bid_sizes = [float(row.get("bid_size") or 0.0) for row in selected]
            ask_sizes = [float(row.get("ask_size") or 0.0) for row in selected]
            timestamp_ms = int(event.get("timestamp") or time.time() * 1000)
        except (TypeError, ValueError, OverflowError):
            return False
        if any(not math.isfinite(x) or x <= 0 for x in bid_prices + ask_prices) or any(not math.isfinite(x) or x < 0 for x in bid_sizes + ask_sizes):
            return False
        if timestamp_ms <= 0:
            return False
        previous_timestamp = self._last_book_timestamp_ms.get(market)
        if previous_timestamp is not None and timestamp_ms < previous_timestamp:
            return False
        if any(left < right for left, right in zip(bid_prices, bid_prices[1:])):
            return False
        if any(left > right for left, right in zip(ask_prices, ask_prices[1:])):
            return False
        if bid_prices[0] > ask_prices[0]:
            return False
        signature = (
            timestamp_ms,
            tuple(bid_prices),
            tuple(bid_sizes),
            tuple(ask_prices),
            tuple(ask_sizes),
        )
        if self._last_book_signature.get(market) == signature:
            return False
        weights = [1.0 / math.sqrt(idx + 1) for idx in range(depth)]
        bid_depth = sum(
            p * s * w for p, s, w in zip(bid_prices, bid_sizes, weights)
        )
        ask_depth = sum(
            p * s * w for p, s, w in zip(ask_prices, ask_sizes, weights)
        )
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total else 0.0
        bid1, ask1 = bid_prices[0], ask_prices[0]
        bid_size1, ask_size1 = bid_sizes[0], ask_sizes[0]
        mid = (bid1 + ask1) / 2.0
        spread_pct = (ask1 - bid1) / mid if mid else 1.0
        size_total = bid_size1 + ask_size1
        micro = (
            (ask1 * bid_size1 + bid1 * ask_size1) / size_total
            if size_total
            else mid
        )
        micro_bias = micro / mid - 1.0 if mid else 0.0
        self._books[market] = BookSnapshot(
            received_at,
            bid_prices,
            bid_sizes,
            ask_prices,
            ask_sizes,
            imbalance,
            micro_bias,
            max(0.0, spread_pct),
        )
        self._last_book_timestamp_ms[market] = timestamp_ms
        self._last_book_signature[market] = signature
        epoch_second = timestamp_ms // 1000
        history = self._book_history[market]
        row = (epoch_second, imbalance, micro_bias)
        if history and history[-1][0] == epoch_second:
            history[-1] = row
        elif not history or epoch_second > history[-1][0]:
            history.append(row)
        return True

    def latest_price(self, market: str) -> float | None:
        frame = self._current.get(market)
        if frame is not None:
            return frame.close
        frames = self._frames.get(market)
        return frames[-1].close if frames else None

    @synchronized
    def book(self, market: str) -> BookSnapshot | None:
        """Return one immutable-by-convention orderbook generation.

        WebSocket callbacks replace snapshots while the engine evaluates an
        exit.  Returning the live list-bearing object without the strategy lock
        made it possible to pair an old bid with the age/spread of a newer
        snapshot.  Snapshot objects are never mutated after publication, so a
        locked reference lets callers derive price, depth, spread and freshness
        from one coherent generation without an unnecessary deep copy.
        """
        return self._books.get(market)

    def book_age(self, market: str) -> float:
        book = self._books.get(market)
        return (
            max(0.0, time.monotonic() - book.received_at)
            if book
            else float("inf")
        )

    def trade_age(self, market: str) -> float:
        received = self._last_trade_received.get(market)
        return (
            max(0.0, time.monotonic() - received)
            if received is not None
            else float("inf")
        )

    @synchronized
    def _series(self, market: str) -> list[SecondFrame]:
        items = list(self._frames.get(market, ()))
        current = self._current.get(market)
        if current is not None:
            items.append(replace(current))
        return items

    def warmup_ratio(self, market: str) -> float:
        frames = self._series(market)
        frame_ratio = len(frames) / max(1, self.config.min_warmup_seconds)
        required_trade_seconds = min(
            max(1, self.config.min_warmup_seconds),
            max(1, self.config.min_warmup_trade_seconds),
        )
        observed_trade_seconds = sum(frame.trades > 0 for frame in frames)
        trade_ratio = observed_trade_seconds / required_trade_seconds
        return _clamp(min(frame_ratio, trade_ratio))

    @staticmethod
    def _returns(frames: list[SecondFrame], window: int) -> list[float]:
        result: list[float] = []
        for idx in range(window, len(frames)):
            base = frames[idx - window].close
            if base > 0:
                result.append(frames[idx].close / base - 1.0)
        return result

    @staticmethod
    def _aggression(frame: SecondFrame) -> float:
        total = frame.bid_value + frame.ask_value
        return (frame.bid_value - frame.ask_value) / total if total else 0.0

    @synchronized
    def _feature_set(self, market: str) -> dict[str, float] | None:
        frames = self._series(market)
        if len(frames) < self.config.min_warmup_seconds:
            return None
        required_trade_seconds = min(
            max(1, self.config.min_warmup_seconds),
            max(1, self.config.min_warmup_trade_seconds),
        )
        if sum(frame.trades > 0 for frame in frames) < required_trade_seconds:
            return None
        aw = max(2, self.config.activity_window_seconds)
        gw = max(2, self.config.aggression_window_seconds)
        mw = max(2, self.config.momentum_window_seconds)
        lw = max(mw + 1, self.config.long_momentum_window_seconds)
        if len(frames) <= lw + 2:
            return None

        values = [f.trade_value for f in frames]
        bids = [f.bid_value for f in frames]
        asks = [f.ask_value for f in frames]
        activity_q = _percentile_rank(
            _rolling_sums(values[:-1], aw), sum(values[-aw:])
        )
        bid_now, ask_now = sum(bids[-gw:]), sum(asks[-gw:])
        flow_total = bid_now + ask_now
        aggression = (bid_now - ask_now) / flow_total if flow_total else 0.0
        # Compare five-second flow with five-second history, not single trades
        # or one-second extremes (which systematically suppress the percentile).
        historical_bids = _rolling_sums(bids[:-1], gw)
        historical_asks = _rolling_sums(asks[:-1], gw)
        aggression_history = [(b-a)/(b+a) for b, a in zip(historical_bids, historical_asks) if b+a > 0]
        aggression_q = (
            _percentile_rank(aggression_history, aggression)
            if aggression > 0
            else 0.0
        )

        short_returns = self._returns(frames, mw)
        long_returns = self._returns(frames, lw)
        if not short_returns or not long_returns:
            return None
        short_now, long_now = short_returns[-1], long_returns[-1]
        momentum_q = (
            _percentile_rank(short_returns[:-1], short_now)
            if short_now > 0 and long_now > 0
            else 0.0
        )

        book = self._books.get(market)
        imbalance = micro_bias = 0.0
        spread_pct = 1.0
        book_q = 0.0
        if book is not None:
            imbalance, micro_bias, spread_pct = (
                book.imbalance,
                book.micro_bias,
                book.spread_pct,
            )
            history = list(self._book_history.get(market, ()))
            minimum_book_observations = max(
                1, int(self.config.min_book_observations)
            )
            if len(history) < minimum_book_observations:
                history = []
            ih = [row[1] for row in history[:-1]]
            mh = [row[2] for row in history[:-1]]
            iq = (
                _percentile_rank(ih, imbalance)
                if history and imbalance > 0
                else 0.0
            )
            mq = (
                _percentile_rank(mh, micro_bias)
                if history and micro_bias > 0
                else 0.0
            )
            delta = 0.0
            dh: list[float] = []
            if len(history) >= 4:
                delta = history[-1][1] - history[-4][1]
                dh = [
                    history[idx][1] - history[idx - 3][1]
                    for idx in range(3, len(history) - 1)
                ]
            dq = _percentile_rank(dh, delta) if delta > 0 else 0.0
            # Stable, historically strong buying pressure is still confirmation.
            # Require ten seconds of fresh, positive book observations, with no
            # net deterioration; a single snapshot or a weakening book cannot qualify.
            recent = [row for row in history if history and row[0] >= history[-1][0] - 10]
            sustained = (
                len(recent) >= 8
                and recent[-1][0] - recent[0][0] >= 10
                and all(0 < b and 0 < m for _, b, m in recent)
                and all(0 < b[0] - a[0] <= 2 for a, b in zip(recent, recent[1:]))
                and recent[-1][1] >= recent[0][1]
                and delta >= 0
            )
            if sustained:
                dq = max(dq, min(iq, mq))
            book_q = (
                (
                    max(1e-6, iq)
                    * max(1e-6, mq)
                    * max(1e-6, dq)
                )
                ** (1.0 / 3.0)
                if history
                else 0.0
            )

        quality = math.prod(
            [
                max(1e-6, activity_q),
                max(1e-6, aggression_q),
                max(1e-6, book_q),
                max(1e-6, momentum_q),
            ]
        ) ** 0.25
        expected_returns = [
            abs(x)
            for x in self._returns(
                frames, self.config.expected_move_window_seconds
            )
            if math.isfinite(x)
        ]
        expected_move = (
            _quantile(expected_returns[-900:], 0.70) if expected_returns else 0.0
        )
        one = self._returns(frames, 1)[-120:]
        sigma = statistics.pstdev(one) if len(one) >= 5 else 0.0
        return {
            "activity_q": activity_q,
            "aggression_q": aggression_q,
            "book_q": book_q,
            "momentum_q": momentum_q,
            "quality": quality,
            "aggression": aggression,
            "imbalance": imbalance,
            "micro_bias": micro_bias,
            "short_return": short_now,
            "long_return": long_now,
            "expected_move": expected_move,
            "realized_30s": sigma * math.sqrt(30.0),
            "spread_pct": spread_pct,
        }

    def hot_score(self, market: str) -> float:
        freshness_limit = max(5.0, self.config.activity_window_seconds * 2.0)
        if self.trade_age(market) > freshness_limit:
            return 0.0
        f = self._feature_set(market)
        if not f:
            return 0.0
        required = (
            f["activity_q"],
            f["aggression_q"],
            f["momentum_q"],
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in required):
            return 0.0
        return (
            f["activity_q"]
            * f["aggression_q"]
            * f["momentum_q"]
        ) ** (1.0 / 3.0) * 100.0

    def rank_markets(
        self, markets: list[str], limit: int
    ) -> list[tuple[str, float]]:
        ranked = [(m, self.hot_score(m)) for m in markets]
        ranked = [x for x in ranked if x[1] > 0]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[: max(1, int(limit))]

    def select_actionable_deep_markets(
        self,
        markets: list[str],
        ranked: list[tuple[str, float]],
        *,
        current_deep: list[str],
        entered_at: dict[str, float],
        now: float,
        stale_limit: float,
        managed: set[str] | None = None,
        scanner_limit: int | None = None,
        deep_limit: int | None = None,
    ) -> CandidateSelection:
        """Select the exact fresh/resident deep set used by live and research.

        Keeping this in the strategy layer prevents recorder/forward validation
        from sampling stale scanner winners that production would replace with
        fresh candidates below the truncated rank list.
        """
        universe = list(dict.fromkeys(markets))
        allowed = set(universe)
        managed_set = set(managed or ())
        scan_cap = max(
            1,
            int(
                self.config.scanner_candidate_count
                if scanner_limit is None
                else scanner_limit
            ),
        )
        deep_cap = max(
            0,
            int(
                self.config.deep_candidate_count
                if deep_limit is None
                else deep_limit
            ),
        )
        freshness = max(0.0, float(stale_limit))
        supplied = {market for market, _ in ranked}
        actionable: list[tuple[str, float]] = []
        stale_skipped = 0
        for market, score in ranked:
            if market in managed_set or market not in allowed:
                continue
            if self.trade_age(market) <= freshness:
                actionable.append((market, float(score)))
            else:
                stale_skipped += 1

        supplemented = 0
        if len(actionable) < scan_cap:
            extras: list[tuple[str, float]] = []
            for market in universe:
                if market in supplied or market in managed_set:
                    continue
                if self.trade_age(market) > freshness:
                    continue
                score = float(self.hot_score(market))
                if math.isfinite(score) and score > 0.0:
                    extras.append((market, score))
            extras.sort(key=lambda item: item[1], reverse=True)
            chosen = extras[: scan_cap - len(actionable)]
            actionable.extend(chosen)
            supplemented = len(chosen)

        actionable.sort(key=lambda item: item[1], reverse=True)
        deduped: list[tuple[str, float]] = []
        seen: set[str] = set()
        for market, score in actionable:
            if market in seen:
                continue
            seen.add(market)
            deduped.append((market, score))
            if len(deduped) >= scan_cap:
                break

        desired = [market for market, _ in deduped[:deep_cap]]
        scores = dict(deduped)
        selected: list[str] = sorted(managed_set)
        for market in current_deep:
            if market in managed_set or market not in allowed:
                continue
            if self.trade_age(market) > freshness:
                continue
            # Keep every fresh incumbent until a desired newcomer actually
            # clears both residency and switch-margin checks below. Dropping an
            # expired incumbent here first would create an empty slot and make
            # deep_switch_margin ineffective.
            selected.append(market)
            if market not in scores:
                incumbent_score = float(self.hot_score(market))
                scores[market] = (
                    incumbent_score
                    if math.isfinite(incumbent_score) and incumbent_score > 0.0
                    else 0.0
                )

        def nonmanaged_count() -> int:
            return sum(1 for market in selected if market not in managed_set)

        for market in desired:
            if market in selected:
                continue
            if nonmanaged_count() < deep_cap:
                selected.append(market)
                continue
            replaceable = [
                current
                for current in selected
                if current not in managed_set
                and current not in desired
                and float(now) - float(entered_at.get(current, now))
                >= self.config.deep_min_residency_seconds
            ]
            if not replaceable:
                continue
            weakest = min(replaceable, key=lambda item: scores.get(item, 0.0))
            if (
                scores.get(market, 0.0)
                >= scores.get(weakest, 0.0) + self.config.deep_switch_margin
            ):
                selected.remove(weakest)
                selected.append(market)

        # A runtime configuration reduction may leave more expired incumbents
        # than the new cap. Residents still complete their minimum stay, while
        # the weakest eligible incumbents are retired deterministically.
        while nonmanaged_count() > deep_cap:
            removable = [
                current
                for current in selected
                if current not in managed_set
                and float(now) - float(entered_at.get(current, now))
                >= self.config.deep_min_residency_seconds
            ]
            if not removable:
                break
            selected.remove(min(removable, key=lambda item: (scores.get(item, 0.0), item)))

        return CandidateSelection(
            selected=sorted(dict.fromkeys(selected)),
            actionable_ranked=deduped,
            stale_skipped=stale_skipped,
            supplemented=supplemented,
        )

    def market_regime(self, markets: list[str]) -> tuple[str, float]:
        universe = list(dict.fromkeys(markets))
        returns: list[float] = []
        freshness_limit = max(10.0, self.config.long_momentum_window_seconds / 2.0)
        for market in universe:
            if self.trade_age(market) > freshness_limit:
                continue
            r = self._returns(self._series(market), 60)
            if r:
                returns.append(r[-1])

        production_universe_size = max(
            1, int(self.config.regime_production_universe_size)
        )
        coverage_required = max(
            production_universe_size,
            math.ceil(
                len(universe) * _clamp(self.config.regime_min_coverage)
            ),
        )
        btc = self._returns(self._series("KRW-BTC"), 60)
        btc_fresh = self.trade_age("KRW-BTC") <= freshness_limit
        if len(returns) < coverage_required or not btc or not btc_fresh:
            return "DATA_INSUFFICIENT", 0.0

        breadth = sum(1 for x in returns if x > 0) / len(returns)
        median = statistics.median(returns)
        btc_now = btc[-1]
        btc_scale = max(
            _quantile([abs(x) for x in btc[-600:]], 0.75) if btc else 0.0,
            1e-6,
        )
        if btc_now < -3.0 * btc_scale and breadth < 0.20:
            return "PANIC", 0.0
        if breadth < 0.40 or median < -0.5 * btc_scale:
            return "RISK_OFF", 0.40
        if breadth > 0.82 and median > 1.2 * btc_scale:
            return "EUPHORIA", 0.65
        if breadth > 0.60 and median > 0:
            return "RISK_ON", 1.0
        return "NEUTRAL", 0.75

    def _is_pullback(self, market: str, expected_move: float) -> bool:
        frames = self._series(market)
        if len(frames) < 65 or expected_move <= 0:
            return False
        recent = frames[-60:]
        low_index = min(range(len(recent)), key=lambda idx: recent[idx].low)
        peak_index = max(range(len(recent)), key=lambda idx: recent[idx].high)
        # A continuation pullback must have an advance first. Looking only at
        # unordered extrema misclassifies peak -> crash -> bounce as PULLBACK.
        if not (low_index < peak_index < len(recent) - 1):
            return False

        pullback_index = min(
            range(peak_index + 1, len(recent)),
            key=lambda idx: recent[idx].low,
        )
        if pullback_index >= len(recent) - 1:
            return False

        low = recent[low_index].low
        peak = recent[peak_index].high
        pullback_low = recent[pullback_index].low
        current = recent[-1].close
        if (
            low <= 0
            or peak <= low
            or pullback_low <= 0
            or current <= pullback_low
            or current >= peak
        ):
            return False
        advance = peak / low - 1.0
        drawdown = peak / pullback_low - 1.0
        remaining_drawdown = peak / current - 1.0
        if advance < expected_move * 1.5 or not (
            expected_move * 0.15 <= drawdown <= expected_move * 0.85
        ):
            return False
        if remaining_drawdown > expected_move * 0.85:
            return False
        short = self._returns(frames, 3)
        return bool(short and short[-1] > 0)

    @synchronized
    def evaluate_entry(
        self,
        market: str,
        *,
        bid_fee: float,
        ask_fee: float,
        health: float,
        regime_factor: float,
    ) -> StrategyDecision:
        f = self._feature_set(market)
        if not f:
            return StrategyDecision(Signal.HOLD, 0.0, "기준 데이터 워밍업 중")
        if self.book_age(market) > 3.0:
            return StrategyDecision(Signal.HOLD, 0.0, "호가 데이터가 오래됨")
        quality = f["quality"]
        if market in self._blocked_after_exit and quality < 0.45:
            self._blocked_after_exit.discard(market)
        expected_move = f["expected_move"]
        spread = f["spread_pct"]
        cost = max(0.0, bid_fee) + max(0.0, ask_fee) + spread * 1.5
        if expected_move <= cost * 2.0:
            return StrategyDecision(
                Signal.HOLD,
                quality * 100.0,
                "예상 움직임이 거래비용 대비 부족",
                expected_move_pct=expected_move,
                round_trip_cost_pct=cost,
            )

        kind = (
            SignalKind.PULLBACK
            if self._is_pullback(market, expected_move)
            else SignalKind.IGNITION
        )
        threshold = (
            self.config.pullback_quality
            if kind == SignalKind.PULLBACK
            else self.config.ignition_quality
        )

        if market in self._blocked_after_exit:
            if quality < 0.45:
                self._blocked_after_exit.discard(market)
            else:
                return StrategyDecision(
                    Signal.HOLD,
                    quality * 100.0,
                    "이전 수급 신호가 아직 초기화되지 않음",
                )
        if f["short_return"] <= 0 or f["long_return"] <= 0:
            return StrategyDecision(
                Signal.HOLD, quality * 100.0, "상승 모멘텀 미확인"
            )
        r30 = self._returns(self._series(market), 30)
        if (
            r30
            and _percentile_rank(r30[:-1], r30[-1]) >= 0.99
            and f["book_q"] < 0.80
        ):
            return StrategyDecision(
                Signal.HOLD, quality * 100.0, "EXTENDED/EXHAUSTED"
            )
        if quality < threshold:
            return StrategyDecision(
                Signal.HOLD, quality * 100.0, f"{kind.value} 품질 대기"
            )

        # ExpectedMove is an absolute-volatility capacity proxy, not a proven
        # directional return forecast. It may decide whether enough movement
        # exists to clear a conservative 2x cost hurdle, but it must not turn a
        # volatile/downward market into near-all-in sizing. Only the excess over
        # that hurdle participates, and live risk budgeting applies stricter
        # equity/open-risk/liquidity constraints afterwards.
        cost_headroom = _clamp(
            (expected_move - 2.0 * cost) / max(expected_move, 1e-9)
        )
        fraction = min(
            _clamp(self.config.max_signal_capital_fraction),
            _clamp(
                cost_headroom
                * quality
                * _clamp(health)
                * _clamp(regime_factor)
            ),
        )
        risk = max(
            cost * 1.8,
            f["realized_30s"] * 1.25,
            spread * 3.0,
        )
        one = [
            abs(x)
            for x in self._returns(self._series(market), 1)[-120:]
            if x != 0
        ]
        typical = statistics.median(one) if one else expected_move / 60.0
        horizon = max(
            20.0, min(600.0, expected_move / max(typical, 1e-6))
        )
        reason = (
            f"{kind.value}: Q={quality:.3f}, "
            f"Activity={f['activity_q']:.2f}, "
            f"Agg={f['aggression_q']:.2f}, "
            f"Book={f['book_q']:.2f}, "
            f"Mom={f['momentum_q']:.2f}"
        )
        return StrategyDecision(
            Signal.BUY,
            quality * 100.0,
            reason,
            kind=kind,
            expected_move_pct=expected_move,
            round_trip_cost_pct=cost,
            initial_risk_pct=risk,
            capital_fraction=fraction,
            expected_horizon_seconds=horizon,
            hold_quality=quality,
        )

    def suggest_emergency_risk(
        self, market: str, round_trip_cost_pct: float
    ) -> float:
        f = self._feature_set(market)
        if not f:
            return max(round_trip_cost_pct * 2.0, 0.0)
        return max(
            round_trip_cost_pct * 1.8,
            f["realized_30s"] * 1.25,
            f["spread_pct"] * 3.0,
        )

    @synchronized
    def evaluate_position(
        self,
        market: str,
        *,
        entry_price: float,
        current_price: float,
        peak_price: float,
        initial_risk_pct: float,
        round_trip_cost_pct: float,
        elapsed_seconds: float,
        expected_horizon_seconds: float,
        trailing_stop_price: float = 0.0,
    ) -> StrategyDecision:
        numeric = (
            entry_price,
            current_price,
            peak_price,
            initial_risk_pct,
            round_trip_cost_pct,
            elapsed_seconds,
            expected_horizon_seconds,
            trailing_stop_price,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            return StrategyDecision(Signal.HOLD, 0.0, "포지션 가격 정보 부족")
        if entry_price <= 0 or current_price <= 0:
            return StrategyDecision(Signal.HOLD, 0.0, "포지션 가격 정보 부족")

        persisted_stop = max(0.0, trailing_stop_price)
        pnl = current_price / entry_price - 1.0
        if initial_risk_pct > 0 and pnl <= -initial_risk_pct:
            return StrategyDecision(
                Signal.SELL,
                0.0,
                f"Emergency Stop {pnl:+.2%}",
                trailing_stop_price=persisted_stop,
            )
        if persisted_stop > 0 and current_price <= persisted_stop:
            return StrategyDecision(
                Signal.SELL,
                0.0,
                f"Ratchet Trailing Stop {current_price:,.8g} <= {persisted_stop:,.8g}",
                trailing_stop_price=persisted_stop,
            )
        if (
            self.config.max_holding_seconds > 0
            and elapsed_seconds >= self.config.max_holding_seconds
        ):
            return StrategyDecision(
                Signal.SELL,
                0.0,
                f"최대 보유시간 {self.config.max_holding_seconds:.0f}초 도달",
                trailing_stop_price=persisted_stop,
            )

        f = self._feature_set(market)
        if not f:
            self._weak_evidence.pop(market, None)
            return StrategyDecision(
                Signal.HOLD,
                0.0,
                "시장 데이터 워밍업/복구 중",
                trailing_stop_price=persisted_stop,
            )

        # Entry scores measure an unusual *acceleration*. A pause in that
        # acceleration is not evidence of an adverse move. In particular,
        # book_q collapses when a still-positive imbalance stops increasing.
        # Holding therefore uses signed flow, signed depth and price support,
        # not entry percentiles or their geometric product.
        noise = max(f['spread_pct'], f['realized_30s'] * 0.25,
                    round_trip_cost_pct * 0.25, 1e-6)
        flow_support = _clamp((f['aggression'] + 1.0) / 2.0)
        book_support = _clamp((f['imbalance'] + 1.0) / 2.0)
        price_support = 0.5 + 0.5 * math.tanh(f['short_return'] / noise)
        hold = 0.4 * flow_support + 0.3 * book_support + 0.3 * price_support

        # Never accumulate weak observations while data is absent/stale.
        # The price-based emergency stop above remains unconditional.
        if self.trade_age(market) > 3.0:
            self._weak_evidence.pop(market, None)
            return StrategyDecision(Signal.HOLD, hold * 100.0,
                                    '보유 관측 대기: 체결 지연 · 수급 청산 확인 초기화',
                                    hold_quality=hold,
                                    trailing_stop_price=persisted_stop)

        peak = max(peak_price, current_price, entry_price)
        mfe = peak / entry_price - 1.0
        if mfe >= round_trip_cost_pct * 2.5:
            trailing = max(
                f["realized_30s"] * 0.45,
                round_trip_cost_pct * 1.5,
                f["spread_pct"] * 2.0,
            )
            persisted_stop = max(
                persisted_stop, peak * (1.0 - min(0.95, trailing))
            )
            if current_price <= persisted_stop:
                return StrategyDecision(
                    Signal.SELL,
                    hold * 100.0,
                    f"Ratchet Trailing: MFE={mfe:+.2%}",
                    hold_quality=hold,
                    trailing_stop_price=persisted_stop,
                )

        if self.book_age(market) > 3.0:
            self._weak_evidence.pop(market, None)
            return StrategyDecision(Signal.HOLD, hold * 100.0,
                                    '보유 관측 대기: 호가 지연 · 수급 청산 확인 초기화',
                                    hold_quality=hold,
                                    trailing_stop_price=persisted_stop)

        deadband = max(0.0, self.config.exit_pressure_deadband)
        adverse = [f['aggression'] < -deadband,
                   f['imbalance'] < -deadband,
                   f['short_return'] < -noise]
        confirmations = 0
        if sum(adverse) >= 2:
            trade_second = self._current[market].second if market in self._current else -1
            books = self._book_history.get(market)
            book_second = books[-1][0] if books else -1
            evidence = self._weak_evidence.get(market)
            new_observation = False
            if evidence is None or elapsed_seconds < evidence[0] or elapsed_seconds - evidence[4] > 3.0:
                evidence = (elapsed_seconds, trade_second, book_second, 1, elapsed_seconds)
            elif trade_second > evidence[1] and book_second > evidence[2]:
                evidence = (evidence[0], trade_second, book_second, evidence[3] + 1, elapsed_seconds)
                new_observation = True
            self._weak_evidence[market] = evidence
            confirmations = evidence[3]
            if (new_observation and trade_second >= 0 and book_second >= 0 and confirmations >= 3
                    and elapsed_seconds - evidence[0] >= self.config.exit_confirmation_seconds):
                return StrategyDecision(Signal.SELL, hold * 100.0,
                                        f'지속 수급 반전: 지표 {sum(adverse)}/3 · 독립 관측 {confirmations}회 · HoldQ={hold:.3f}',
                                        hold_quality=hold,
                                        trailing_stop_price=persisted_stop)
        else:
            self._weak_evidence.pop(market, None)

        if (
            expected_horizon_seconds > 0
            and elapsed_seconds > expected_horizon_seconds * 1.5
            # A past MFE must not exempt a position forever. Judge whether the
            # currently executable price still pays for the round trip.
            and pnl < round_trip_cost_pct * 0.5
            and (hold < 0.60 or f['short_return'] <= 0)
        ):
            return StrategyDecision(
                Signal.SELL,
                hold * 100.0,
                "기대 시간 내 모멘텀 미발생",
                hold_quality=hold,
                trailing_stop_price=persisted_stop,
            )
        return StrategyDecision(
            Signal.HOLD,
            hold * 100.0,
            f"보유 유지: HoldQ={hold:.3f} · 반전 지표 {sum(adverse)}/3 · 확인 {confirmations}회",
            hold_quality=hold,
            trailing_stop_price=persisted_stop,
        )

    @synchronized
    def notify_exit(self, market: str) -> None:
        self._blocked_after_exit.add(market)
        self._weak_evidence.pop(market, None)

    @synchronized
    def reset_orderbook(self, market: str) -> None:
        self._books.pop(market, None)
        self._book_history.pop(market, None)
        self._last_book_timestamp_ms.pop(market, None)
        self._last_book_signature.pop(market, None)
        self._weak_evidence.pop(market, None)

    @synchronized
    def reset_trades(self, markets: list[str] | tuple[str, ...] | set[str]) -> None:
        """Invalidate feature continuity after a public-stream reconnect."""
        for market in markets:
            self._frames.pop(market, None)
            self._current.pop(market, None)
            self._last_trade_received.pop(market, None)
            self._last_trade_timestamp_ms.pop(market, None)
            self._last_trade_sequence.pop(market, None)
            self._last_trade_signature.pop(market, None)
            self._weak_evidence.pop(market, None)
