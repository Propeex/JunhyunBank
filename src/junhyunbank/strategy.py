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
        self._weak_counts: dict[str, int] = defaultdict(int)
        self._last_trade_received: dict[str, float] = {}
        self._blocked_after_exit: set[str] = set()

    @synchronized
    def on_trade(self, event: dict[str, Any]) -> None:
        market = str(event.get("code") or "")
        price = float(event.get("trade_price") or 0.0)
        volume = float(event.get("trade_volume") or 0.0)
        if not market or not math.isfinite(price) or not math.isfinite(volume) or price <= 0 or volume <= 0:
            return

        timestamp_ms = int(event.get("trade_timestamp") or event.get("timestamp") or time.time() * 1000)
        second = timestamp_ms // 1000
        side = str(event.get("ask_bid") or "").upper()
        value = price * volume
        frame = self._current.get(market)

        if frame is not None and second < frame.second:
            # 과거/역순 이벤트가 현재 시간 프레임을 훼손하지 않게 버린다.
            return

        self._last_trade_received[market] = time.monotonic()

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

    @synchronized
    def on_orderbook(self, event: dict[str, Any]) -> None:
        market = str(event.get("code") or "")
        units = event.get("orderbook_units") or []
        if not market or not isinstance(units, list) or not units:
            return
        depth = max(1, min(self.config.orderbook_depth, len(units)))
        selected = units[:depth]
        bid_prices = [float(row.get("bid_price") or 0.0) for row in selected]
        ask_prices = [float(row.get("ask_price") or 0.0) for row in selected]
        bid_sizes = [float(row.get("bid_size") or 0.0) for row in selected]
        ask_sizes = [float(row.get("ask_size") or 0.0) for row in selected]
        if any(not math.isfinite(x) or x <= 0 for x in bid_prices + ask_prices) or any(not math.isfinite(x) or x < 0 for x in bid_sizes + ask_sizes):
            return
        if bid_prices[0] > ask_prices[0]:
            return
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
            time.monotonic(),
            bid_prices,
            bid_sizes,
            ask_prices,
            ask_sizes,
            imbalance,
            micro_bias,
            max(0.0, spread_pct),
        )
        epoch_second = int(event.get("timestamp") or time.time() * 1000) // 1000
        history = self._book_history[market]
        row = (epoch_second, imbalance, micro_bias)
        if history and history[-1][0] == epoch_second:
            history[-1] = row
        elif not history or epoch_second > history[-1][0]:
            history.append(row)

    def latest_price(self, market: str) -> float | None:
        frame = self._current.get(market)
        if frame is not None:
            return frame.close
        frames = self._frames.get(market)
        return frames[-1].close if frames else None

    def book(self, market: str) -> BookSnapshot | None:
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
        return _clamp(
            len(self._series(market)) / max(1, self.config.min_warmup_seconds)
        )

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
            ih = [row[1] for row in history[:-1]]
            mh = [row[2] for row in history[:-1]]
            iq = _percentile_rank(ih, imbalance) if imbalance > 0 else 0.0
            mq = _percentile_rank(mh, micro_bias) if micro_bias > 0 else 0.0
            delta = 0.0
            dh: list[float] = []
            if len(history) >= 4:
                delta = history[-1][1] - history[-4][1]
                dh = [
                    history[idx][1] - history[idx - 3][1]
                    for idx in range(3, len(history) - 1)
                ]
            dq = _percentile_rank(dh, delta) if delta > 0 else 0.0
            book_q = (
                max(1e-6, iq) * max(1e-6, mq) * max(1e-6, dq)
            ) ** (1.0 / 3.0)

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
        return (
            max(1e-6, f["activity_q"])
            * max(1e-6, f["aggression_q"])
            * max(1e-6, f["momentum_q"])
        ) ** (1.0 / 3.0) * 100.0

    def rank_markets(
        self, markets: list[str], limit: int
    ) -> list[tuple[str, float]]:
        ranked = [(m, self.hot_score(m)) for m in markets]
        ranked = [x for x in ranked if x[1] > 0]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[: max(1, int(limit))]

    def market_regime(self, markets: list[str]) -> tuple[str, float]:
        returns: list[float] = []
        freshness_limit = max(10.0, self.config.long_momentum_window_seconds / 2.0)
        for market in markets:
            if self.trade_age(market) > freshness_limit:
                continue
            r = self._returns(self._series(market), 60)
            if r:
                returns.append(r[-1])
        if len(returns) < 10:
            return "NEUTRAL", 0.70
        breadth = sum(1 for x in returns if x > 0) / len(returns)
        median = statistics.median(returns)
        btc = self._returns(self._series("KRW-BTC"), 60)
        btc_now = btc[-1] if btc and self.trade_age("KRW-BTC") <= freshness_limit else 0.0
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
        low = min(f.low for f in recent)
        peak = max(f.high for f in recent)
        current = recent[-1].close
        if low <= 0 or peak <= low or current <= 0:
            return False
        advance = peak / low - 1.0
        drawdown = peak / current - 1.0
        if advance < expected_move * 1.5 or not (
            expected_move * 0.15 <= drawdown <= expected_move * 0.85
        ):
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

        edge = _clamp((expected_move - cost) / max(expected_move, 1e-9))
        fraction = _clamp(
            edge * quality * _clamp(health) * _clamp(regime_factor)
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
    ) -> StrategyDecision:
        if entry_price <= 0 or current_price <= 0:
            return StrategyDecision(Signal.HOLD, 0.0, "포지션 가격 정보 부족")

        f = self._feature_set(market)
        pnl = current_price / entry_price - 1.0
        if initial_risk_pct > 0 and pnl <= -initial_risk_pct:
            return StrategyDecision(
                Signal.SELL, 0.0, f"Emergency Stop {pnl:+.2%}"
            )
        if not f:
            return StrategyDecision(
                Signal.HOLD, 0.0, "시장 데이터 워밍업/복구 중"
            )

        parts = [
            max(1e-6, f["activity_q"]),
            max(1e-6, f["aggression_q"]),
            max(1e-6, f["book_q"]),
            max(1e-6, f["momentum_q"]),
        ]
        hold = math.prod(parts) ** 0.25
        if hold < 0.36 or (f["aggression"] < 0 and f["imbalance"] < 0):
            self._weak_counts[market] += 1
        else:
            self._weak_counts[market] = 0
        if self._weak_counts[market] >= 2:
            return StrategyDecision(
                Signal.SELL,
                hold * 100.0,
                f"수급 약화: HoldQ={hold:.3f}",
                hold_quality=hold,
            )

        peak = max(peak_price, current_price, entry_price)
        mfe = peak / entry_price - 1.0
        if mfe >= round_trip_cost_pct * 2.5:
            trailing = max(
                f["realized_30s"] * 0.45,
                round_trip_cost_pct * 1.5,
                f["spread_pct"] * 2.0,
            )
            if current_price <= peak * (1.0 - trailing):
                return StrategyDecision(
                    Signal.SELL,
                    hold * 100.0,
                    f"Adaptive Trailing: MFE={mfe:+.2%}",
                    hold_quality=hold,
                )

        if (
            expected_horizon_seconds > 0
            and elapsed_seconds > expected_horizon_seconds * 1.5
            and mfe < round_trip_cost_pct * 1.5
            and hold < 0.60
        ):
            return StrategyDecision(
                Signal.SELL,
                hold * 100.0,
                "기대 시간 내 모멘텀 미발생",
                hold_quality=hold,
            )
        return StrategyDecision(
            Signal.HOLD,
            hold * 100.0,
            f"보유 유지: HoldQ={hold:.3f}",
            hold_quality=hold,
        )

    @synchronized
    def notify_exit(self, market: str) -> None:
        self._blocked_after_exit.add(market)
        self._weak_counts.pop(market, None)

    @synchronized
    def reset_orderbook(self, market: str) -> None:
        self._books.pop(market, None)
        self._book_history.pop(market, None)
