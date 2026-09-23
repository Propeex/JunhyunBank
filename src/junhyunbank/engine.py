from __future__ import annotations

import math
import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import __version__

from .config import AppConfig
from .execution import OrderExecution
from .health import StrategyHealthGovernor
from .market_stream import MarketStream
from .models import EngineState, Position, Signal
from .risk import RiskManager
from .storage import Storage
from .strategy import BookSnapshot, MicroFlowStrategy
from .upbit import UpbitAPIError, UpbitClient


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    """A coherent, immutable executable-side orderbook generation."""

    received_at: float
    bid_prices: tuple[float, ...]
    bid_sizes: tuple[float, ...]
    ask_price: float
    spread_pct: float
    source: str

    @property
    def bid(self) -> float:
        return self.bid_prices[0]

    @property
    def token(self) -> tuple[Any, ...]:
        return (
            self.source,
            self.received_at,
            self.bid_prices,
            self.bid_sizes,
            self.ask_price,
            self.spread_pct,
        )


class EventBuffer:
    """Bounded UI event buffer with latest-value coalescing.

    Market callbacks must never block on a paused/slow GUI. High-frequency
    state snapshots replace their older value by key, while infrequent log and
    lifecycle events retain FIFO ordering within a hard memory bound.
    """

    _COALESCED_TYPES = {
        "price",
        "entry_diagnostic",
        "position_diagnostic",
        "runtime_health",
        "portfolio",
        "strategy_health",
        "candidates",
        "stream_status",
        "deep_set",
        "private_asset",
    }

    def __init__(self, max_items: int = 5_000) -> None:
        self.max_items = max(100, int(max_items))
        self._fifo: deque[dict[str, Any]] = deque()
        self._latest: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._dropped = 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def qsize(self) -> int:
        with self._lock:
            return len(self._fifo) + len(self._latest)

    @staticmethod
    def _key(event: dict[str, Any]) -> tuple[str, str]:
        event_type = str(event.get("type") or "")
        identity = str(event.get("market") or event.get("name") or "")
        return event_type, identity

    def _make_room(self) -> None:
        if len(self._fifo) + len(self._latest) < self.max_items:
            return
        if self._latest:
            self._latest.popitem(last=False)
        elif self._fifo:
            self._fifo.popleft()
        self._dropped += 1

    def put(self, event: dict[str, Any]) -> None:
        with self._lock:
            event_type = str(event.get("type") or "")
            if event_type in self._COALESCED_TYPES:
                key = self._key(event)
                if key in self._latest:
                    self._latest[key] = event
                    self._latest.move_to_end(key)
                    return
                self._make_room()
                self._latest[key] = event
                return
            self._make_room()
            self._fifo.append(event)

    def get_nowait(self) -> dict[str, Any]:
        with self._lock:
            if self._fifo:
                return self._fifo.popleft()
            if self._latest:
                _, event = self._latest.popitem(last=False)
                return event
        raise queue.Empty


class TradingEngine(OrderExecution):
    def __init__(self, client: UpbitClient, config: AppConfig | None = None, storage: Storage | None = None) -> None:
        self.client = client
        self.config = config or AppConfig()
        self.storage = storage or Storage()
        self.strategy = MicroFlowStrategy(self.config.strategy)
        self.risk = RiskManager(self.config.safety)
        self.health_governor = StrategyHealthGovernor(minimum_samples=max(8, self.config.strategy.health_lookback // 5))
        self.events = EventBuffer(self.config.safety.event_buffer_max_items)
        self._reported_event_drops = 0
        self._hard_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = EngineState.STOPPED
        self._global_streams: list[MarketStream] = []
        self._deep_stream: MarketStream | None = None
        self._allowed_markets: list[str] = []
        self._deep_markets: list[str] = []
        self._deep_entered_at: dict[str, float] = {}
        self._latest_prices: dict[str, float] = {}
        self._fallback_best_bids: dict[str, ExecutableQuote] = {}
        self._last_managed_quote_fallback = 0.0
        self._last_price_ui_emit: dict[str, float] = {}
        self._scan_seconds = self._evaluation_seconds = 0.0
        self._last_runtime_sample = 0.0
        self._evaluation_cycles = 0
        self._order_lock = threading.Lock()
        # Linearizes stop transitions against the last pre-POST check. An order
        # already inside this gate is considered in flight; a stop that acquires
        # it first makes every subsequently prepared intent reject locally.
        self._submission_gate = threading.RLock()
        self._fee_cache: dict[str, tuple[float, float, float, float, float]] = {}
        self._session_start_equity: float | None = None
        self._session_strategy_pnl_start: float | None = None
        self._session_drawdown_halted = False
        self._session_entry_locked = False
        self._session_entry_lock_reason = ""
        self._update_shutdown = False
        self._run_started_at = 0.0
        self._last_no_candidate_warning = 0.0
        self._entry_messages = {}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def accepting_entries(self) -> bool:
        return bool(
            self._state == EngineState.RUNNING
            and not self.risk.emergency
            and not self._session_drawdown_halted
            and not self._session_entry_locked
        )

    def wait(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0.0, timeout))

    def start(self) -> None:
        if self.running:
            return
        if not self.client.access_key or not self.client.secret_key:
            raise RuntimeError("업비트 API 키가 필요합니다.")
        self.strategy = MicroFlowStrategy(self.config.strategy)
        self._allowed_markets = []
        self._deep_markets = []
        self._deep_entered_at.clear()
        self._latest_prices.clear()
        self._fallback_best_bids.clear()
        self._last_managed_quote_fallback = 0.0
        self._entry_messages.clear()
        self._fee_cache.clear()
        self._last_price_ui_emit.clear()
        self._evaluation_cycles = 0
        self._scan_seconds = self._evaluation_seconds = 0.0
        self._last_runtime_sample = 0.0
        self._market_discovery_retry_at = 0.0
        self._market_discovery_ready = False
        self.risk.reset_session()
        self._hard_stop.clear()
        self._update_shutdown = False
        self._run_started_at = time.monotonic()
        self._last_no_candidate_warning = 0.0
        self._session_start_equity = None
        self._session_strategy_pnl_start = None
        self._session_drawdown_halted = False
        self._session_entry_locked = False
        self._session_entry_lock_reason = ""

        # A terminal SELL may have reached Upbit just before the previous
        # process stopped.  Reconcile it before inspecting balances or building
        # the session PnL baseline; otherwise the durable managed row can make a
        # legitimately empty account look inconsistent and prevent recovery.
        self._reconcile_orders()

        # A submitted intent that still cannot be reconciled may fill after
        # this process has started.  Building a zero-position baseline in that
        # state would let a delayed BUY profit offset losses opened during this
        # session (or otherwise mix two accounting epochs).  Exit management
        # is still safe and must remain available, but entries stay locked
        # until the operator restarts after reconciliation succeeds.
        unresolved_orders = self.storage.pending_orders()
        if unresolved_orders:
            self._session_entry_locked = True
            self._session_entry_lock_reason = (
                f"시작 시 미확정 주문 {len(unresolved_orders)}건이 남아 손익 기준을 확정할 수 없음"
            )

        positions: list[Position] = []
        if not self._session_entry_locked:
            try:
                equity, _, _, positions = self._live_portfolio()
                self._session_start_equity = equity
            except Exception as exc:
                self.risk.report_api_failure()
                self._session_entry_locked = True
                self._session_entry_lock_reason = (
                    f"시작 시 계정 기준 확인 실패: {exc}"
                )
        # Establish the session baseline from the same executable-bid source
        # used by the running loss gate. A last-trade ticker can sit above the
        # best bid and would otherwise manufacture an immediate session loss
        # as soon as the first orderbook arrives.
        if not self._session_entry_locked:
            managed_markets = sorted(self.storage.managed_markets())
            self._refresh_managed_fallback_quotes(managed_markets)
            strategy_pnl = self._strategy_pnl_snapshot(positions)
            if (
                strategy_pnl is None
                or self._session_start_equity is None
                or not math.isfinite(self._session_start_equity)
                or self._session_start_equity <= 0
            ):
                self._session_entry_locked = True
                self._session_entry_lock_reason = (
                    "시작 시 관리 전략 손익/자본 기준을 안전하게 구성할 수 없음"
                )
            else:
                self._session_strategy_pnl_start = strategy_pnl
        self._state = EngineState.RUNNING
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()
        if self._session_entry_locked:
            self._emit(
                "warning",
                "LIVE 청산 관리는 시작했지만 이번 세션의 신규 매수는 영구 잠금되었습니다. "
                f"프로그램을 종료하고 원인을 확인한 뒤 다시 시작하세요. ({self._session_entry_lock_reason})",
                version=__version__,
            )
        else:
            self._emit(
                "status",
                f"LIVE 자동매매 시작 · V{__version__}",
                version=__version__,
            )

    def request_stop(self) -> None:
        with self._submission_gate:
            if not self.running:
                self._state = EngineState.STOPPED
                already_stopped = True
            elif self._state == EngineState.DRAINING:
                return
            else:
                self._state = EngineState.DRAINING
                already_stopped = False
        if already_stopped:
            self._emit("status", "이미 자동매매가 종료되어 있습니다.")
            return
        self._emit("status", "종료 대기: 신규 매수를 중단하고 JunhyunBank 보유 포지션의 전략 청산을 기다립니다.")

    def emergency_stop(self) -> None:
        # The gate is the linearization boundary: if stop acquires it first no
        # later POST can start; if a submit already owns it, that request is
        # explicitly in flight and its response is durably recorded first.
        with self._submission_gate:
            self._hard_stop.set()
            self.risk.trigger_emergency()
            self._state = EngineState.STOPPED
        self._emit("emergency", "긴급 정지: 신규 주문과 전략 실행을 즉시 중단했습니다. 보유 포지션은 강제 청산하지 않습니다.")

    def shutdown_for_update(self) -> None:
        with self._submission_gate:
            self._hard_stop.set()
            self._update_shutdown = True
            self._state = EngineState.STOPPED
        self._emit("status", "업데이트를 위해 엔진을 일시 정지합니다. 관리 포지션은 재시작 후 이어서 관리합니다.")

    def drain_events(self, limit: int = 200) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        dropped = self.events.dropped
        if dropped > self._reported_event_drops and len(items) < max(1, limit):
            delta = dropped - self._reported_event_drops
            self._reported_event_drops = dropped
            items.append(
                {
                    "type": "warning",
                    "message": (
                        f"UI 이벤트 적체로 오래된 화면 갱신 {delta}건을 폐기했습니다. "
                        "거래 판단 상태는 엔진 내부/DB에 유지됩니다."
                    ),
                    "dropped": dropped,
                }
            )
        return items

    def _emit(self, event_type: str, message: str, **payload: Any) -> None:
        event = {"type": event_type, "message": message, **payload}
        self.events.put(event)
        try:
            self.storage.event("INFO", event_type, message, payload or None)
        except Exception:
            pass

    def _entry_status(self, market: str, reason: str, **metrics: Any) -> None:
        now = time.monotonic()
        last_reason, last_at = self._entry_messages.get(market, ('', -60.0))
        self.events.put({'type': 'entry_diagnostic', 'market': market, 'reason': reason, **metrics})
        if now-last_at >= 60.0 or (reason != last_reason and now-last_at >= 10.0):
            self._entry_messages[market] = (reason, now)
            self._emit('entry_wait', f'{market}: {reason}', **metrics)

    @staticmethod
    def _is_warning_market(row: dict[str, Any]) -> bool:
        event = row.get("market_event")
        if isinstance(event, dict):
            if bool(event.get("warning")):
                return True
            caution = event.get("caution")
            if isinstance(caution, dict) and any(bool(v) for v in caution.values()):
                return True
            if bool(caution):
                return True
        warning = str(row.get("market_warning", "")).upper()
        return warning not in ("", "NONE")

    @staticmethod
    def _chunks(values: list[str], size: int) -> list[list[str]]:
        return [values[i:i + size] for i in range(0, len(values), size)]

    def _refresh_markets(self) -> None:
        rows = self.client.get_markets()
        allowed = sorted(str(row["market"]) for row in rows if str(row.get("market", "")).startswith("KRW-") and not self._is_warning_market(row))
        if allowed == self._allowed_markets:
            return
        self._allowed_markets = allowed
        self._restart_global_streams()
        self._emit("market_universe", f"KRW 실시간 감시 종목 {len(allowed)}개", count=len(allowed))

    def _stream_status(self, payload: dict[str, Any]) -> None:
        self.events.put({"type": "stream_status", **payload})

    def _restart_global_streams(self) -> None:
        for stream in self._global_streams:
            stream.stop()
        self._global_streams.clear()
        for index, markets in enumerate(self._chunks(sorted(set(self._allowed_markets) | self.storage.managed_markets()), 100)):
            stream = MarketStream(
                markets,
                on_trade=self._on_trade,
                on_error=lambda message, idx=index: self._emit("warning", f"시장 스트림 {idx + 1}: {message}"),
                on_status=self._stream_status,
                name=f"upbit-trades-{index + 1}",
                max_event_lag_seconds=self.config.safety.market_data_stale_seconds,
            )
            stream.start()
            self._global_streams.append(stream)

    def _select_deep_markets(self, ranked: list[tuple[str, float]]) -> list[str]:
        now = time.monotonic()
        managed = self.storage.managed_markets()
        desired = [market for market, _ in ranked[: self.config.strategy.deep_candidate_count] if market in self._allowed_markets]
        scores = dict(ranked)
        selected: list[str] = sorted(managed)

        for market in self._deep_markets:
            if market in managed or market not in self._allowed_markets:
                continue
            age = now - self._deep_entered_at.get(market, now)
            if market in desired or age < self.config.strategy.deep_min_residency_seconds:
                selected.append(market)

        def nonmanaged_count() -> int:
            return sum(1 for market in selected if market not in managed)

        for market in desired:
            if market in selected:
                continue
            if nonmanaged_count() < self.config.strategy.deep_candidate_count:
                selected.append(market)
                continue
            replaceable = [
                current
                for current in selected
                if current not in managed
                and current not in desired
                and now - self._deep_entered_at.get(current, now) >= self.config.strategy.deep_min_residency_seconds
            ]
            if not replaceable:
                continue
            weakest = min(replaceable, key=lambda current: scores.get(current, 0.0))
            if scores.get(market, 0.0) >= scores.get(weakest, 0.0) + self.config.strategy.deep_switch_margin:
                selected.remove(weakest)
                selected.append(market)

        return sorted(dict.fromkeys(selected))

    def _restart_deep_stream(self, markets: list[str]) -> None:
        markets = sorted(dict.fromkeys(markets))
        if markets == self._deep_markets:
            return
        previous = set(self._deep_markets)
        if self._deep_stream:
            self._deep_stream.stop()
            self._deep_stream = None
        self._deep_markets = markets
        now = time.monotonic()
        self._deep_entered_at = {
            market: self._deep_entered_at.get(market, now)
            for market in markets
        }
        if markets:
            self._deep_stream = MarketStream(
                markets,
                on_orderbook=self._on_orderbook,
                on_error=lambda message: self._emit("warning", f"호가 스트림: {message}"),
                on_status=self._stream_status,
                orderbook_depth=max(5, self.config.strategy.orderbook_depth),
                name="upbit-orderbook-deep",
                max_event_lag_seconds=self.config.safety.market_data_stale_seconds,
            )
            self._deep_stream.start()
        added = sorted(set(markets) - previous)
        removed = sorted(previous - set(markets))
        if added or removed:
            self.events.put({"type": "deep_set", "added": added, "removed": removed, "count": len(markets)})

    def _on_trade(self, event: dict[str, Any]) -> None:
        # The strategy is the canonical ordering/deduplication gate. A rejected
        # frame must not overwrite the engine price cache with older data.
        if not self.strategy.on_trade(event):
            return
        market = str(event.get("code") or "")
        try:
            price = float(event.get("trade_price") or 0.0)
        except (TypeError, ValueError, OverflowError):
            return
        if not market or not math.isfinite(price) or price <= 0:
            return
        self._latest_prices[market] = price
        now = time.monotonic()
        if now - self._last_price_ui_emit.get(market, 0.0) >= 0.25:
            self._last_price_ui_emit[market] = now
            self.events.put({"type": "price", "market": market, "price": price,
                             "timestamp": float(event.get("trade_timestamp") or event.get("timestamp") or time.time() * 1000) / 1000.0})

    def _on_orderbook(self, event: dict[str, Any]) -> None:
        self.strategy.on_orderbook(event)

    def _global_market_data_ready(self) -> bool:
        """Fail closed when any production trade stream is disconnected/stale.

        Unit fixtures do not create streams, so an empty list deliberately
        remains neutral. A live engine always creates at least one stream after
        market discovery.
        """
        if not self._global_streams:
            return True
        maximum_age = self.config.safety.market_data_stale_seconds
        return all(
            stream.connected and stream.age_seconds <= maximum_age
            for stream in self._global_streams
        )

    def _refresh_managed_fallback_quotes(self, markets: list[str]) -> None:
        """Refresh executable best bids for exits when the deep WS is stale."""
        markets = sorted(dict.fromkeys(markets))
        if not markets or not callable(getattr(self.client, "get_orderbooks", None)):
            return
        now = time.monotonic()
        interval = max(0.25, self.config.safety.managed_quote_fallback_seconds)
        if now - self._last_managed_quote_fallback < interval:
            return
        self._last_managed_quote_fallback = now
        try:
            accepted_quotes = 0
            for chunk in self._chunks(markets, 100):
                rows = self.client.get_orderbooks(chunk)
                # Age is measured when the full response actually arrives.
                # Measuring before a slow request hides its entire transport
                # delay and can turn an old quote into an apparently fresh one.
                received_wall_ms = time.time() * 1000.0
                received_monotonic = time.monotonic()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    market = str(row.get("market") or row.get("code") or "")
                    units = row.get("orderbook_units") or []
                    try:
                        timestamp_ms = float(row.get("timestamp"))
                        depth = max(
                            1,
                            min(self.config.strategy.orderbook_depth, len(units)),
                        )
                        selected = units[:depth]
                        bid_prices = tuple(
                            float(unit.get("bid_price")) for unit in selected
                        )
                        bid_sizes = tuple(
                            float(unit.get("bid_size")) for unit in selected
                        )
                        ask_prices = tuple(
                            float(unit.get("ask_price")) for unit in selected
                        )
                    except (IndexError, AttributeError, TypeError, ValueError, OverflowError):
                        continue
                    age = (received_wall_ms - timestamp_ms) / 1000.0
                    if (
                        market in markets
                        and bid_prices
                        and len(bid_prices) == len(bid_sizes) == len(ask_prices)
                        and all(math.isfinite(value) and value > 0 for value in bid_prices)
                        and all(math.isfinite(value) and value >= 0 for value in bid_sizes)
                        and all(math.isfinite(value) and value > 0 for value in ask_prices)
                        and not any(
                            left < right
                            for left, right in zip(bid_prices, bid_prices[1:])
                        )
                        and not any(
                            left > right
                            for left, right in zip(ask_prices, ask_prices[1:])
                        )
                        and ask_prices[0] >= bid_prices[0]
                        and math.isfinite(age)
                        and -2.0 <= age <= self.config.safety.market_data_stale_seconds
                    ):
                        mid = (bid_prices[0] + ask_prices[0]) / 2.0
                        spread = (
                            (ask_prices[0] - bid_prices[0]) / mid
                            if mid > 0
                            else 1.0
                        )
                        self._fallback_best_bids[market] = ExecutableQuote(
                            received_at=received_monotonic - max(0.0, age),
                            bid_prices=bid_prices,
                            bid_sizes=bid_sizes,
                            ask_price=ask_prices[0],
                            spread_pct=max(0.0, spread),
                            source="REST orderbook",
                        )
                        accepted_quotes += 1
            if accepted_quotes == 0:
                raise ValueError("요청한 시장의 신선한 매수호가가 응답에 없음")
            self.risk.report_api_success()
        except Exception as exc:
            self.risk.report_api_failure()
            self._entry_status("전체", f"관리 포지션 REST 호가 확인 실패: {exc}")

    @staticmethod
    def _quote_from_book(book: BookSnapshot, source: str) -> ExecutableQuote | None:
        bid_prices = tuple(book.bid_prices)
        bid_sizes = tuple(book.bid_sizes)
        if (
            not bid_prices
            or len(bid_prices) != len(bid_sizes)
            or not book.ask_prices
            or any(not math.isfinite(value) or value <= 0 for value in bid_prices)
            or any(not math.isfinite(value) or value < 0 for value in bid_sizes)
            or any(left < right for left, right in zip(bid_prices, bid_prices[1:]))
            or not math.isfinite(book.ask_prices[0])
            or book.ask_prices[0] < bid_prices[0]
            or not math.isfinite(book.received_at)
            or book.received_at <= 0
        ):
            return None
        mid = (book.ask_prices[0] + bid_prices[0]) / 2.0
        spread_pct = (book.ask_prices[0] - bid_prices[0]) / mid
        return ExecutableQuote(
            received_at=book.received_at,
            bid_prices=bid_prices,
            bid_sizes=bid_sizes,
            ask_price=book.ask_prices[0],
            spread_pct=spread_pct,
            source=source,
        )

    def _fresh_executable_quote(self, market: str) -> ExecutableQuote | None:
        """Read bid, depth, spread and age from exactly one generation."""
        maximum_age = self.config.safety.market_data_stale_seconds
        book = self.strategy.book(market)
        if book is not None:
            quote = self._quote_from_book(book, "websocket orderbook")
            age = (
                time.monotonic() - quote.received_at
                if quote is not None
                else float("inf")
            )
            if (
                quote is not None
                and 0.0 <= age <= maximum_age
            ):
                return quote
        fallback = self._fallback_best_bids.get(market)
        fallback_age = (
            time.monotonic() - fallback.received_at
            if fallback is not None
            else float("inf")
        )
        if (
            fallback is not None
            and 0.0 <= fallback_age <= maximum_age
        ):
            return fallback
        return None

    def _fresh_executable_bid(self, market: str) -> tuple[float | None, str]:
        quote = self._fresh_executable_quote(market)
        return (quote.bid, quote.source) if quote is not None else (None, "stale")

    def _fresh_spread_pct(self, market: str) -> float | None:
        quote = self._fresh_executable_quote(market)
        return quote.spread_pct if quote is not None else None

    @staticmethod
    def _finite_age(value: float) -> float | None:
        return value if math.isfinite(value) else None

    def _publish_activity(self) -> None:
        try:
            snapshot = self.storage.activity_snapshot()
        except Exception as exc:
            # The dashboard is optional; a read failure must not interrupt
            # order supervision or prevent the final stopped notification.
            self.events.put({'type': 'warning', 'message': f'실제 체결 화면 갱신 실패: {exc}'})
            return
        self.events.put({'type': 'activity', **snapshot,
                         'evaluation_cycles': self._evaluation_cycles})

    def _publish_runtime_health(self, ranked: list[tuple[str, float]]) -> None:
        self._publish_activity()
        tracked = sum(1 for market in self._allowed_markets if self.strategy.latest_price(market))
        history_ready = [
            market
            for market in self._allowed_markets
            if self.strategy.warmup_ratio(market) >= 1.0
        ]
        warmed = sum(self.strategy.trade_age(market) <= 30.0 for market in history_ready)
        global_total = len(self._global_streams)
        global_connected = sum(1 for stream in self._global_streams if stream.connected)
        global_ages = [stream.age_seconds for stream in self._global_streams]
        trade_age = max(global_ages) if global_ages else float("inf")
        deep_connected = bool(self._deep_stream and self._deep_stream.connected)
        deep_age = self._deep_stream.age_seconds if self._deep_stream else float("inf")
        limit = self.config.safety.market_data_stale_seconds
        fresh_deep = sum(self.strategy.trade_age(m) <= limit and self.strategy.book_age(m) <= limit for m in self._deep_markets)
        self.events.put(
            {
                "type": "runtime_health",
                "global_connected": global_connected,
                "global_total": global_total,
                "trade_age": self._finite_age(trade_age),
                "deep_connected": deep_connected,
                "deep_age": self._finite_age(deep_age),
                "deep_markets": len(self._deep_markets),
                "tracked_markets": tracked,
                "warmed_markets": warmed,
                "history_ready_markets": len(history_ready),
                "allowed_markets": len(self._allowed_markets),
                "candidate_count": len(ranked),
                "elapsed": max(0.0, time.monotonic() - self._run_started_at),
                "scan_seconds": self._scan_seconds,
                "evaluation_seconds": self._evaluation_seconds,
                "fresh_deep_markets": fresh_deep,
            }
        )
        now = time.monotonic()
        if now - self._last_runtime_sample >= 60.0:
            self._last_runtime_sample = now
            self._emit('runtime_sample',
                       f'처리 상태: 최신 체결·호가 {fresh_deep}/{len(self._deep_markets)}개 · 후보 계산 {self._scan_seconds:.3f}초 · 주문 판단 {self._evaluation_seconds:.3f}초',
                       version=__version__, scan_seconds=self._scan_seconds,
                       evaluation_seconds=self._evaluation_seconds, fresh_deep_markets=fresh_deep,
                       deep_markets=len(self._deep_markets), trade_age=self._finite_age(trade_age),
                       book_age=self._finite_age(deep_age), global_connected=global_connected,
                       deep_connected=deep_connected, warmed_markets=warmed,
                       history_ready_markets=len(history_ready))

    def _run(self) -> None:
        next_market_refresh = next_candidate_refresh = next_evaluation = next_portfolio = next_runtime_health = 0.0
        last_ranked: list[tuple[str, float]] = []
        try:
            while not self._hard_stop.is_set():
                now = time.monotonic()
                if now >= next_market_refresh:
                    try:
                        self._refresh_markets(); self.risk.report_api_success()
                    except Exception as exc:
                        self.risk.report_api_failure(); self._emit("warning", f"시장 목록 갱신 실패: {exc}")
                    next_market_refresh = now + (self.config.strategy.market_refresh_seconds if getattr(self, '_market_discovery_ready', True) else 10.0)
                if now >= next_candidate_refresh:
                    scan_started = time.monotonic()
                    last_ranked = self.strategy.rank_markets(self._allowed_markets, self.config.strategy.scanner_candidate_count)
                    self._scan_seconds = time.monotonic() - scan_started
                    deep = self._select_deep_markets(last_ranked)
                    self._restart_deep_stream(deep)
                    top = last_ranked[:12]
                    self.events.put(
                        {
                            "type": "candidates",
                            "markets": [market for market, _ in top],
                            "scores": {market: score for market, score in top},
                            "prices": {market: self._latest_prices.get(market) for market, _ in top},
                            "tracked": len(self._latest_prices),
                        }
                    )
                    elapsed = now - self._run_started_at
                    if not last_ranked and elapsed >= self.config.strategy.no_candidate_warning_seconds:
                        if now - self._last_no_candidate_warning >= 60.0:
                            self._last_no_candidate_warning = now
                            self._emit("warning", "5분 이상 후보가 없습니다. 정상 대기보다 실시간 체결 수신/워밍업 상태를 먼저 확인하세요.")
                    next_candidate_refresh = now + self.config.strategy.candidate_refresh_seconds
                if now >= next_runtime_health:
                    self._publish_runtime_health(last_ranked)
                    next_runtime_health = now + self.config.strategy.runtime_health_seconds
                if now >= next_evaluation:
                    evaluation_started = time.monotonic()
                    self._evaluate_cycle()
                    self._evaluation_cycles += 1
                    self._evaluation_seconds = time.monotonic() - evaluation_started
                    next_evaluation = now + self.config.strategy.evaluation_seconds
                if now >= next_portfolio:
                    try:
                        self._publish_portfolio()
                    except Exception as exc:
                        self._emit("warning", f"자산 갱신 실패: {exc}")
                    next_portfolio = now + self.config.strategy.portfolio_publish_seconds
                if self._state == EngineState.DRAINING and not self.storage.drain_blocking_markets() and not self.storage.pending_orders():
                    residual = self.storage.managed_markets()
                    # The last sell may have settled after this cycle's runtime
                    # snapshot. Publish it before the UI receives drain_complete.
                    self._publish_activity()
                    message = "자동 청산 가능한 관리 포지션 처리가 완료되어 자동매매를 종료합니다."
                    if residual:
                        message += f" DUST/격리 상태 {len(residual)}건은 기록을 보존했으므로 수동 확인이 필요합니다."
                    self._emit("drain_complete", message, residual_markets=sorted(residual))
                    break
                self._hard_stop.wait(0.10)
        except Exception as exc:
            self._emit("error", f"자동매매 엔진 중단: {exc}")
        finally:
            for stream in self._global_streams:
                stream.stop()
            self._global_streams.clear()
            if self._deep_stream:
                self._deep_stream.stop(); self._deep_stream = None
            self._state = EngineState.STOPPED
            self._publish_activity()
            if not self._update_shutdown:
                self._emit("stopped", "자동매매 엔진 종료")

    def _fee_info(self, market: str) -> tuple[float, float, float, float] | None:
        cached = self._fee_cache.get(market); now = time.monotonic()
        if cached and now - cached[0] < self.config.strategy.fee_cache_seconds:
            return cached[1], cached[2], cached[3], cached[4]
        try:
            chance = self.client.get_order_chance(market)
            if any(key not in chance or chance[key] is None for key in ('bid_fee', 'ask_fee')):
                raise ValueError('계정 수수료 정보 누락')
            bid_fee, ask_fee = float(chance.get("bid_fee") or 0.0), float(chance.get("ask_fee") or 0.0)
            if not all(math.isfinite(f) and 0 <= f < 1 for f in (bid_fee, ask_fee)):
                raise ValueError('계정 수수료 정보 오류')
            market_info = chance.get("market") or {}
            min_bid = float((market_info.get("bid") or {}).get("min_total") or self.config.safety.min_order_krw)
            min_ask = float((market_info.get("ask") or {}).get("min_total") or self.config.safety.min_order_krw)
            if not all(math.isfinite(value) and value > 0 for value in (min_bid, min_ask)):
                raise ValueError('계정 최소 주문금액 정보 오류')
            self._fee_cache[market] = (now, bid_fee, ask_fee, min_bid, min_ask)
            self.risk.report_api_success()
            return bid_fee, ask_fee, min_bid, min_ask
        except Exception as exc:
            self.risk.report_api_failure(); self._emit("warning", f"{market} 주문 가능정보 조회 실패: {exc}"); return None

    def _exit_fee_info(self, market: str, state: dict[str, Any]) -> tuple[float, float]:
        """Return sell fee/minimum without putting a REST lookup in the exit path.

        The actual entry fee is durable position state and is normally equal to
        the account's sell fee. A previously validated order-chance cache is
        even better. Either source is preferable to delaying a stop decision by
        up to several REST timeouts. The exchange remains authoritative and can
        reject a now-invalid minimum; reconciliation keeps that result auditable.
        """
        cached = self._fee_cache.get(market)
        if cached:
            ask_fee, min_ask = float(cached[2]), float(cached[4])
            if (
                math.isfinite(ask_fee)
                and 0 <= ask_fee < 1
                and math.isfinite(min_ask)
                and min_ask > 0
            ):
                return ask_fee, min_ask
        try:
            entry_fee = float(state.get("entry_fee_rate") or 0.0005)
        except (TypeError, ValueError):
            entry_fee = 0.0005
        if not math.isfinite(entry_fee) or not 0 <= entry_fee < 1:
            entry_fee = 0.0005
        return entry_fee, self.config.safety.min_order_krw

    def _live_portfolio(self) -> tuple[float, float, float, list[Position]]:
        accounts = self.client.get_accounts(); self.risk.report_api_success()
        available_cash = total_cash = 0.0; positions: list[Position] = []
        for row in accounts:
            currency = str(row.get("currency", ""))
            balance = float(row.get("balance") or 0.0)
            locked = float(row.get("locked") or 0.0)
            if not all(math.isfinite(value) and value >= 0 for value in (balance, locked)):
                raise ValueError(f"{currency or 'unknown'} 잔고 숫자 형식 오류")
            if currency == "KRW":
                available_cash, total_cash = balance, balance + locked; continue
            if balance + locked <= 0 or str(row.get("unit_currency") or "KRW") != "KRW":
                continue
            average = float(row.get("avg_buy_price") or 0.0)
            if not math.isfinite(average) or average < 0:
                raise ValueError(f"{currency} 평균단가 숫자 형식 오류")
            positions.append(Position(f"KRW-{currency}", balance, average, locked))
        missing = [p.market for p in positions if not self._latest_prices.get(p.market)]
        for chunk in self._chunks(missing, 100):
            for row in self.client.get_tickers(chunk):
                market, price = str(row.get("market") or ""), float(row.get("trade_price") or 0.0)
                if market and math.isfinite(price) and price > 0:
                    self._latest_prices[market] = price
        equity = total_cash + sum(p.market_value(self._latest_prices.get(p.market, p.avg_price)) for p in positions)
        if not math.isfinite(equity) or equity < 0:
            raise ValueError("계정 평가액 계산 결과가 유효하지 않습니다.")
        return equity, available_cash, total_cash, positions

    def _strategy_pnl_snapshot(self, positions: list[Position]) -> float | None:
        """Mark only JunhyunBank-managed exposure, excluding user cash/assets."""
        total = self.storage.strategy_realized_pnl_total()
        if not math.isfinite(total):
            return None
        positions_by_market = {position.market: position for position in positions}
        for market in self.storage.managed_markets():
            state = self.storage.get_managed_state(market)
            position = positions_by_market.get(market)
            if state is None or position is None:
                return None
            # QUARANTINED means the durable lot can no longer be matched to an
            # account balance with confidence. Valuing it as though ownership
            # were proven could hide losses and reopen the entry gate.
            status = str(state.get("status") or "ACTIVE").upper()
            if status not in {"ACTIVE", "DUST"}:
                return None
            quantity = self._finite_float(state.get("managed_quantity"))
            entry = self._finite_float(state.get("entry_price"))
            entry_amount = self._finite_float(state.get("entry_amount_krw"))
            realized = self._finite_float(state.get("realized_pnl_krw"), 0.0)
            entry_fee = self._finite_float(state.get("entry_fee_rate"), 0.0005)
            if (
                quantity is None
                or quantity <= 0
                or entry is None
                or entry <= 0
                or entry_amount is None
                or entry_amount <= 0
                or realized is None
                or entry_fee is None
                or not 0 <= entry_fee < 1
                or position.total_quantity
                + max(1e-12, quantity * 1e-10)
                < quantity
            ):
                return None
            quote = self._fresh_executable_quote(market)
            if quote is None or quote.bid <= 0:
                return None
            # Best+IOC is a limit order at the current best opposing price.  It
            # cannot assume liquidity at worse levels, so valuing the entire lot
            # at bid1 is safe only when bid1 itself can absorb the managed qty.
            depth_tolerance = max(1e-12, quantity * 1e-10)
            if not quote.bid_sizes or quote.bid_sizes[0] + depth_tolerance < quantity:
                return None
            exit_fee, _ = self._exit_fee_info(market, state)
            total += realized + quantity * (
                quote.bid * (1.0 - exit_fee) - entry * (1.0 + entry_fee)
            )
        return total if math.isfinite(total) else None

    def _strategy_session_return(
        self, positions: list[Position], *, equity_krw: float
    ) -> float | None:
        snapshot = self._strategy_pnl_snapshot(positions)
        if snapshot is None:
            return None
        if self._session_strategy_pnl_start is None:
            self._session_strategy_pnl_start = snapshot
        denominator = self._session_start_equity
        if denominator is None:
            denominator = equity_krw
            self._session_start_equity = denominator
        if not math.isfinite(denominator) or denominator <= 0:
            return None
        value = (snapshot - self._session_strategy_pnl_start) / denominator
        return value if math.isfinite(value) else None

    @staticmethod
    def _created_elapsed(state: dict[str, Any] | None) -> float:
        try:
            created = datetime.fromisoformat(str((state or {}).get("created_at") or ""))
            return max(0.0, (datetime.now() - created).total_seconds())
        except Exception:
            return 0.0

    @staticmethod
    def _finite_float(value: Any, default: float | None = None) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @staticmethod
    def _order_average_price(detail: dict[str, Any]) -> tuple[float, float, float]:
        executed = float(detail.get("executed_volume") or 0.0); paid_fee = float(detail.get("paid_fee") or 0.0); funds = 0.0
        for trade in detail.get("trades") or []:
            if not isinstance(trade, dict):
                continue
            funds += float(trade.get("funds") or 0.0) if trade.get("funds") is not None else float(trade.get("price") or 0.0) * float(trade.get("volume") or 0.0)
        return executed, funds / executed if executed > 0 and funds > 0 else 0.0, paid_fee

    def _settle_order(self, accepted: dict[str, Any]) -> dict[str, Any]:
        order_id = str(accepted.get("uuid") or "")
        if not order_id:
            return accepted
        detail = accepted; deadline = time.monotonic() + self.config.safety.settlement_grace_seconds
        while time.monotonic() < deadline and not self._hard_stop.is_set():
            try:
                detail = self.client.get_order(uuid_value=order_id); self.risk.report_api_success()
            except Exception as exc:
                self.risk.report_api_failure(); self._emit("warning", f"주문 {order_id} 체결조회 실패: {exc}"); time.sleep(0.25); continue
            if str(detail.get("state") or "") in {"done", "cancel"}:
                break
            time.sleep(0.20)
        return detail

    @staticmethod
    def _simulate_buy_slippage(book: BookSnapshot, amount_krw: float) -> tuple[float, float]:
        remaining = max(0.0, amount_krw); spent = volume = 0.0
        for price, size in zip(book.ask_prices, book.ask_sizes):
            take = min(remaining, price * size)
            if take <= 0: continue
            volume += take / price; spent += take; remaining -= take
            if remaining <= 1e-9: break
        if volume <= 0 or not book.ask_prices: return 0.0, 0.0
        return max(0.0, spent / volume / book.ask_prices[0] - 1.0), spent

    @staticmethod
    def _simulate_sell_slippage(book: BookSnapshot, amount_krw: float) -> tuple[float, float]:
        remaining = max(0.0, amount_krw); proceeds = volume = 0.0
        for price, size in zip(book.bid_prices, book.bid_sizes):
            take = min(remaining, price * size)
            if take <= 0: continue
            volume += take / price; proceeds += take; remaining -= take
            if remaining <= 1e-9: break
        if volume <= 0 or not book.bid_prices: return 0.0, 0.0
        return max(0.0, 1.0 - proceeds / volume / book.bid_prices[0]), proceeds

    @staticmethod
    def _liquidity_capacity(book: BookSnapshot, *, expected_move_pct: float, bid_fee: float, ask_fee: float) -> float:
        if not book.ask_prices: return 0.0
        edge_room = max(0.0, expected_move_pct - bid_fee - ask_fee - book.spread_pct)
        allowed_slippage = edge_room * 0.30; best = book.ask_prices[0]; capacity = 0.0
        for price, size in zip(book.ask_prices, book.ask_sizes):
            if price <= 0 or price / best - 1.0 > allowed_slippage: break
            capacity += price * size
        return capacity

    @staticmethod
    def _entry_book_token(book: BookSnapshot) -> tuple[Any, ...]:
        """Identify one coherent entry-orderbook generation."""
        return (
            book.received_at,
            tuple(book.bid_prices),
            tuple(book.bid_sizes),
            tuple(book.ask_prices),
            tuple(book.ask_sizes),
            book.spread_pct,
        )

    def _fresh_entry_book(self, market: str) -> BookSnapshot | None:
        """Return a structurally valid, fresh two-sided entry snapshot."""
        book = self.strategy.book(market)
        if book is None:
            return None
        age = time.monotonic() - book.received_at
        values = (
            list(book.bid_prices)
            + list(book.bid_sizes)
            + list(book.ask_prices)
            + list(book.ask_sizes)
            + [book.spread_pct]
        )
        if (
            not math.isfinite(book.received_at)
            or book.received_at <= 0
            or not 0.0 <= age <= self.config.safety.market_data_stale_seconds
            or not book.bid_prices
            or not book.ask_prices
            or len(book.bid_prices) != len(book.bid_sizes)
            or len(book.ask_prices) != len(book.ask_sizes)
            or any(not math.isfinite(value) for value in values)
            or any(value <= 0 for value in book.bid_prices + book.ask_prices)
            or any(value < 0 for value in book.bid_sizes + book.ask_sizes)
            or book.bid_prices[0] > book.ask_prices[0]
            or book.spread_pct < 0
        ):
            return None
        return book

    def _evaluate_cycle(self) -> None:
        self._reconcile_orders()
        pending = self.storage.pending_orders()
        pending_markets = {o['market'] for o in pending}
        try:
            equity, available_cash, _, positions = self._live_portfolio()
        except Exception as exc:
            self.risk.report_api_failure(); self._entry_status("전체", f"잔고 조회 실패 · 자동 재시도: {exc}"); return
        positions_by_market = {p.market: p for p in positions}
        managed = self.storage.managed_markets()
        stale_managed = [
            market
            for market in managed
            if self.strategy.book_age(market) > self.config.safety.market_data_stale_seconds
        ]
        self._refresh_managed_fallback_quotes(stale_managed)
        with self._order_lock:
            for market in sorted(managed):
                if market in pending_markets: continue
                if self._hard_stop.is_set() or self.risk.emergency: return
                state = self.storage.get_managed_state(market)
                if state is None:
                    continue
                position = positions_by_market.get(market)
                elapsed = self._created_elapsed(state)
                status = str(state.get("status") or "ACTIVE").upper()
                if position is None or position.total_quantity <= 0:
                    if elapsed <= self.config.safety.settlement_grace_seconds:
                        continue
                    absent = self.storage.record_managed_absence(
                        market,
                        quarantine_after=self.config.safety.managed_absence_confirmations,
                    ) or state
                    count = int(absent.get("absence_count") or 0)
                    if str(absent.get("status") or "").upper() == "QUARANTINED":
                        self._entry_status(
                            market,
                            "반복 잔고 불일치로 관리 포지션 격리 · 자동 해제/매도 금지, 수동 확인 필요",
                            absence_count=count,
                        )
                    else:
                        self._entry_status(
                            market,
                            "관리 기록과 계정 잔고 불일치 · 다음 독립 잔고 조회로 재확인",
                            absence_count=count,
                        )
                    continue
                recorded_status = self.storage.mark_managed_seen(market)
                if recorded_status == "QUARANTINED" or status == "QUARANTINED":
                    self._entry_status(
                        market,
                        "격리된 관리 포지션 · 사용자 잔고와 혼동하지 않도록 자동매도 금지, 수동 확인 필요",
                    )
                    continue
                managed_qty = self._finite_float(state.get("managed_quantity"), 0.0) or 0.0
                if managed_qty <= 0:
                    self._entry_status(market, "관리 수량 미확인 · 계정 잔고를 자동관리 수량으로 추정하지 않습니다."); continue
                quantity_tolerance = max(1e-12, managed_qty * 1e-10)
                if position.total_quantity + quantity_tolerance < managed_qty:
                    self.storage.quarantine_managed_position(market)
                    self._entry_status(
                        market,
                        "계정 총수량이 영속 관리수량보다 작아 소유권 불명 · 즉시 격리, 자동매도 금지",
                        account_quantity=position.total_quantity,
                        managed_quantity=managed_qty,
                    )
                    continue
                quote = self._fresh_executable_quote(market)
                if quote is None:
                    self._entry_status(
                        market,
                        "실행 가능한 최신 매수호가 없음 · 오래된 체결가로 청산하지 않고 REST/WS 복구 대기",
                        book_age=self._finite_age(self.strategy.book_age(market)),
                    )
                    continue
                current_price, quote_source = quote.bid, quote.source
                entry_price = self._finite_float(state.get("entry_price"))
                if entry_price is None or entry_price <= 0:
                    self.storage.quarantine_managed_position(market)
                    self._entry_status(
                        market,
                        "영속 진입 원가가 유효하지 않아 계정 평균단가로 추정하지 않고 격리 · 자동매도 금지",
                    )
                    continue
                entry_amount = self._finite_float(state.get("entry_amount_krw"))
                entry_fee = self._finite_float(state.get("entry_fee_rate"))
                realized_pnl = self._finite_float(state.get("realized_pnl_krw"))
                if (
                    entry_amount is None
                    or entry_amount <= 0
                    or entry_fee is None
                    or not 0 <= entry_fee < 1
                    or realized_pnl is None
                ):
                    self.storage.quarantine_managed_position(market)
                    self._entry_status(
                        market,
                        "영속 원금/수수료/실현손익 회계 필드가 손상되어 격리 · 손실 누락 방지를 위해 자동매도 금지",
                    )
                    continue
                fresh_trade = self.strategy.latest_price(market)
                peak_observation = current_price
                if (
                    fresh_trade is not None
                    and math.isfinite(fresh_trade)
                    and fresh_trade > 0
                    and self.strategy.trade_age(market) <= self.config.safety.market_data_stale_seconds
                ):
                    peak_observation = fresh_trade
                stored_peak = self._finite_float(state.get("peak_price"), entry_price) or entry_price
                peak = max(stored_peak, peak_observation)
                self.storage.update_managed_peak(market, peak)
                ask_fee, min_ask = self._exit_fee_info(market, state)
                if managed_qty * current_price < min_ask:
                    self.storage.mark_managed_dust(market)
                    self._entry_status(
                        market,
                        "관리 잔여수량이 최소 매도금액 미만 · DUST로 기록 보존, 자동 종료 대기에서 제외",
                        value_krw=managed_qty * current_price,
                        min_ask_krw=min_ask,
                    )
                    continue
                if status == "DUST":
                    # A remainder can become orderable again after a price rise
                    # or exchange-minimum change. It must rejoin DRAINING rather
                    # than remain permanently excluded as historical dust.
                    self.storage.mark_managed_tradeable(market)
                    state["status"] = "ACTIVE"
                spread = quote.spread_pct
                round_trip = entry_fee + ask_fee + spread * 1.5
                initial_risk = self._finite_float(state.get("initial_risk_pct"), 0.0) or 0.0
                if initial_risk <= 0:
                    initial_risk = self.strategy.suggest_emergency_risk(market, round_trip)
                    if initial_risk > 0: self.storage.mark_managed_position(market, initial_risk_pct=initial_risk)
                if not math.isfinite(initial_risk) or initial_risk <= 0:
                    self.storage.quarantine_managed_position(market)
                    self._entry_status(
                        market,
                        "영속 초기위험을 복구할 수 없어 격리 · 자동 청산 정책을 안전하게 계산할 수 없음",
                    )
                    continue
                horizon = self._finite_float(state.get("expected_horizon_seconds"), 300.0) or 300.0
                trailing_stop = self._finite_float(state.get("trailing_stop_price"), 0.0) or 0.0
                decision = self.strategy.evaluate_position(
                    market,
                    entry_price=entry_price,
                    current_price=current_price,
                    peak_price=peak,
                    initial_risk_pct=initial_risk,
                    round_trip_cost_pct=round_trip,
                    elapsed_seconds=elapsed,
                    expected_horizon_seconds=horizon,
                    trailing_stop_price=trailing_stop,
                )
                if decision.trailing_stop_price > trailing_stop:
                    self.storage.update_managed_trailing_stop(market, decision.trailing_stop_price)
                self.events.put({'type': 'position_diagnostic', 'market': market, 'reason': decision.reason,
                                 'score': decision.score, 'elapsed': elapsed,
                                 'quote_source': quote_source,
                                 'trailing_stop_price': decision.trailing_stop_price})
                now = time.monotonic()
                key = f'position:{market}'
                previous, logged_at = self._entry_messages.get(key, ('', -60.0))
                if now - logged_at >= 60.0 or (previous != decision.reason and now - logged_at >= 10.0) or decision.signal == Signal.SELL:
                    self._entry_messages[key] = (decision.reason, now)
                    self._emit('position_state', f'{market}: {decision.reason}', elapsed_seconds=elapsed, hold_quality=decision.hold_quality)
                if decision.signal == Signal.SELL:
                    # Re-read the executable quote and re-run the policy at the
                    # final sell boundary. Earlier managed markets, storage I/O
                    # or a slow host must not turn a once-fresh decision into an
                    # order based on an expired/changed bid.
                    final_quote = self._fresh_executable_quote(market)
                    if final_quote is None:
                        self._entry_status(
                            market,
                            "청산 직전 실행호가가 만료되어 매도 보류 · 최신 WS/REST 호가 재확인",
                        )
                        continue
                    final_price = final_quote.bid
                    final_source = final_quote.source
                    final_spread = final_quote.spread_pct
                    final_peak = max(peak, final_price)
                    self.storage.update_managed_peak(market, final_peak)
                    final_round_trip = entry_fee + ask_fee + final_spread * 1.5
                    final_decision = self.strategy.evaluate_position(
                        market,
                        entry_price=entry_price,
                        current_price=final_price,
                        peak_price=final_peak,
                        initial_risk_pct=initial_risk,
                        round_trip_cost_pct=final_round_trip,
                        elapsed_seconds=elapsed,
                        expected_horizon_seconds=horizon,
                        trailing_stop_price=max(
                            trailing_stop, decision.trailing_stop_price
                        ),
                    )
                    if final_decision.trailing_stop_price > trailing_stop:
                        self.storage.update_managed_trailing_stop(
                            market, final_decision.trailing_stop_price
                        )
                    self.events.put(
                        {
                            'type': 'position_diagnostic',
                            'market': market,
                            'reason': final_decision.reason,
                            'score': final_decision.score,
                            'elapsed': elapsed,
                            'quote_source': final_source,
                            'trailing_stop_price': final_decision.trailing_stop_price,
                            'final_recheck': True,
                        }
                    )
                    if final_decision.signal != Signal.SELL:
                        self._entry_status(
                            market,
                            f"청산 직전 재확인 해제: {final_decision.reason}",
                        )
                        continue
                    def sell_pre_submit_check() -> bool:
                        """Re-evaluate at the serialized POST boundary."""
                        guard_started_at = time.monotonic()
                        guard_quote = self._fresh_executable_quote(market)
                        if guard_quote is None:
                            self._entry_status(
                                market,
                                "POST 직전 실행호가 재검증 만료 · 매도 보류",
                                quote_source="stale",
                            )
                            return False
                        guard_price = guard_quote.bid
                        guard_source = guard_quote.source
                        guard_spread = guard_quote.spread_pct
                        if managed_qty * guard_price < min_ask:
                            self.storage.mark_managed_dust(market)
                            self._entry_status(
                                market,
                                "POST 직전 가치가 최소 매도금액 미만 · DUST로 보존",
                                value_krw=managed_qty * guard_price,
                                min_ask_krw=min_ask,
                            )
                            return False
                        guard_peak = max(final_peak, guard_price)
                        self.storage.update_managed_peak(market, guard_peak)
                        guard_decision = self.strategy.evaluate_position(
                            market,
                            entry_price=entry_price,
                            current_price=guard_price,
                            peak_price=guard_peak,
                            initial_risk_pct=initial_risk,
                            round_trip_cost_pct=(
                                entry_fee + ask_fee + guard_spread * 1.5
                            ),
                            elapsed_seconds=elapsed,
                            expected_horizon_seconds=horizon,
                            trailing_stop_price=max(
                                trailing_stop,
                                decision.trailing_stop_price,
                                final_decision.trailing_stop_price,
                            ),
                        )
                        if guard_decision.trailing_stop_price > trailing_stop:
                            self.storage.update_managed_trailing_stop(
                                market, guard_decision.trailing_stop_price
                            )
                        guard_elapsed = time.monotonic() - guard_started_at
                        if guard_elapsed > max(
                            0.0, self.config.safety.order_guard_seconds
                        ):
                            self._entry_status(
                                market,
                                "POST 직전 청산 재판단 시간 초과 · 매도 보류",
                                quote_source=guard_source,
                                guard_elapsed=guard_elapsed,
                            )
                            return False
                        if guard_decision.signal != Signal.SELL:
                            self._entry_status(
                                market,
                                f"POST 직전 청산 재확인 해제: {guard_decision.reason}",
                                quote_source=guard_source,
                            )
                            return False
                        confirmed_quote = self._fresh_executable_quote(market)
                        if (
                            confirmed_quote is None
                            or confirmed_quote.token != guard_quote.token
                        ):
                            self._entry_status(
                                market,
                                "POST 직전 판단 중 호가 세대가 변경되어 매도 보류 · 다음 주기에 재평가",
                                quote_source=guard_source,
                            )
                            return False
                        return True

                    self._sell_managed(
                        market=market,
                        position=position,
                        managed_qty=managed_qty,
                        current_price=final_price,
                        min_ask_krw=min_ask,
                        ask_fee=ask_fee,
                        state=state or {},
                        reason=final_decision.reason,
                        pre_submit_check=sell_pre_submit_check,
                    )
            if self._state != EngineState.RUNNING or self.risk.emergency: return
            if self._session_entry_locked:
                self._entry_status(
                    '전체',
                    '시작 시 손익/자본 기준 확인 실패로 이번 세션 신규 매수 영구 잠금 · 청산 관리만 계속',
                )
                return
            session_return = self._strategy_session_return(
                positions, equity_krw=equity
            )
            if session_return is None:
                self._entry_status(
                    '전체',
                    '관리 전략 손익을 신선한 실행호가로 계산할 수 없어 신규 매수 차단',
                )
                return
            drawdown_limit = max(
                0.0, self.config.safety.session_drawdown_halt_fraction
            )
            if drawdown_limit > 0 and session_return <= -drawdown_limit:
                self._session_drawdown_halted = True
            if self._session_drawdown_halted:
                self._entry_status(
                    '전체',
                    (
                        '관리 전략 세션 손실 한도 도달 · 신규 매수 잠금 유지 '
                        f'(전략 손익률 {session_return:.2%}, 한도 {-drawdown_limit:.2%})'
                    ),
                )
                return
            health = self.health_governor.score(self.storage.strategy_outcomes(self.config.strategy.health_lookback)); regime_name, regime_factor = self.strategy.market_regime(self._allowed_markets)
            self.events.put({"type": "strategy_health", "health": health, "regime": regime_name})
            if pending:
                self._entry_status('전체', f'미확정 주문 {len(pending)}건 확인 중 · 신규 매수 차단'); return
            if not getattr(self, '_market_discovery_ready', True):
                self._entry_status('전체', '시장 경보 목록 갱신 실패 · 재확인 전 신규 매수 차단'); return
            if not self._global_market_data_ready():
                self._entry_status('전체', '전체 체결 스트림 일부가 끊겼거나 지연됨 · 시장 국면 신뢰성 복구 전 신규 매수 차단'); return
            if health <= 0:
                self._entry_status('전체', '전략 건강도 0 · 최근 실거래 손실로 신규 매수 중단 · 성과 검토 필요'); return
            if regime_factor <= 0:
                self._entry_status('전체', f'시장 국면 {regime_name} · 신규 매수 차단'); return
            if not self._deep_markets:
                self._entry_status('전체', '진입 후보 대기 · 시세 연결/3분 워밍업 상태를 확인하세요.'); return
            self._entry_status('전체', '진입 조건 평가 중 · 아래 종목별 대기 이유를 확인하세요.')
            held = set(positions_by_market); decisions: list[tuple[str, Any, tuple[float, float, float, float]]] = []
            uncached_fee_lookups = 0
            for market, _ in self.strategy.rank_markets(self._deep_markets, self.config.strategy.deep_candidate_count):
                if market not in self._allowed_markets: continue
                if market in held or market in managed:
                    self._entry_status(market, '이미 보유 중 · 추가 매수 제외'); continue
                if self._state != EngineState.RUNNING or self.risk.emergency: return
                if max(self.strategy.trade_age(market), self.strategy.book_age(market)) > self.config.safety.market_data_stale_seconds:
                    self._entry_status(market, '체결/호가 수신 대기 또는 3초 이상 지연',
                                       trade_age=self._finite_age(self.strategy.trade_age(market)),
                                       book_age=self._finite_age(self.strategy.book_age(market))); continue
                cached_fee = self._fee_cache.get(market)
                fee_cache_fresh = bool(
                    cached_fee
                    and time.monotonic() - cached_fee[0] < self.config.strategy.fee_cache_seconds
                )
                if not fee_cache_fresh:
                    if uncached_fee_lookups >= max(0, self.config.strategy.max_fee_lookups_per_cycle):
                        self._entry_status(
                            market,
                            '주문 가능정보 순차 확인 대기 · 다음 평가 주기로 이월',
                        )
                        continue
                    uncached_fee_lookups += 1
                fee_info = self._fee_info(market)
                if not fee_info: continue
                bid_fee, ask_fee, _, _ = fee_info
                decision = self.strategy.evaluate_entry(market, bid_fee=bid_fee, ask_fee=ask_fee, health=health, regime_factor=regime_factor)
                self._entry_status(market, decision.reason, score=decision.score,
                                   expected_move_pct=decision.expected_move_pct, cost_pct=decision.round_trip_cost_pct)
                if decision.signal == Signal.BUY: decisions.append((market, decision, fee_info))
            decisions.sort(key=lambda item: (item[1].capital_fraction, item[1].score), reverse=True)
            cash_remaining = available_cash
            gross_exposure = sum(
                position.market_value(
                    max(
                        position.avg_price,
                        float(self._latest_prices.get(position.market) or position.avg_price),
                    )
                )
                for position in positions
            )
            open_risk = 0.0
            for owned_market in managed:
                owned = self.storage.get_managed_state(owned_market) or {}
                owned_quantity = float(owned.get("managed_quantity") or 0.0)
                owned_entry = float(owned.get("entry_price") or 0.0)
                owned_risk = float(owned.get("initial_risk_pct") or 0.0)
                if all(math.isfinite(value) and value >= 0 for value in (owned_quantity, owned_entry, owned_risk)):
                    open_risk += owned_quantity * owned_entry * owned_risk
            for market, decision, fee_info in decisions:
                if self._state != EngineState.RUNNING or self.risk.emergency or self.storage.pending_orders(): return
                # Earlier candidates may have waited behind fee lookups or
                # another order. Fresh quotes alone do not keep an old signal valid.
                decision = self.strategy.evaluate_entry(market, bid_fee=fee_info[0], ask_fee=fee_info[1], health=health, regime_factor=regime_factor)
                if decision.signal != Signal.BUY:
                    self._entry_status(market, f'주문 직전 재확인: {decision.reason}', score=decision.score,
                                       expected_move_pct=decision.expected_move_pct, cost_pct=decision.round_trip_cost_pct)
                    continue
                bid_fee, ask_fee, min_bid, _ = fee_info; book = self.strategy.book(market)
                if not book: continue
                capacity = self._liquidity_capacity(book, expected_move_pct=decision.expected_move_pct, bid_fee=bid_fee, ask_fee=ask_fee)
                planned_risk = max(decision.initial_risk_pct, decision.round_trip_cost_pct * 1.8)
                budget = self.risk.entry_budget(
                    equity_krw=equity,
                    available_cash_krw=cash_remaining,
                    gross_exposure_krw=gross_exposure,
                    open_risk_krw=open_risk,
                    initial_risk_pct=planned_risk,
                    liquidity_capacity_krw=capacity,
                    signal_fraction=decision.capital_fraction,
                    session_return_pct=session_return,
                    min_order_krw=min_bid,
                )
                if not budget.allowed:
                    self._entry_status(
                        market,
                        f'매수 차단: {budget.reason}',
                        limiting_factor=budget.limiting_factor,
                        amount_krw=budget.amount_krw,
                        min_order_krw=max(
                            min_bid, self.config.safety.min_order_krw
                        ),
                        available_cash=cash_remaining,
                        capital_fraction=decision.capital_fraction,
                    )
                    continue
                amount = min(budget.amount_krw, cash_remaining / (1.0 + bid_fee))
                buy_slip, fillable = self._simulate_buy_slippage(book, amount); sell_slip, _ = self._simulate_sell_slippage(book, amount); amount = min(amount, fillable)
                actual_cost = bid_fee + ask_fee + book.spread_pct + buy_slip + sell_slip
                actual_risk = max(planned_risk, actual_cost * 1.8)
                if actual_risk > planned_risk:
                    budget = self.risk.entry_budget(
                        equity_krw=equity,
                        available_cash_krw=cash_remaining,
                        gross_exposure_krw=gross_exposure,
                        open_risk_krw=open_risk,
                        initial_risk_pct=actual_risk,
                        liquidity_capacity_krw=capacity,
                        signal_fraction=decision.capital_fraction,
                        session_return_pct=session_return,
                        min_order_krw=min_bid,
                    )
                    if not budget.allowed:
                        self._entry_status(
                            market,
                            f'매수 차단: {budget.reason}',
                            limiting_factor=budget.limiting_factor,
                            amount_krw=budget.amount_krw,
                            min_order_krw=max(
                                min_bid, self.config.safety.min_order_krw
                            ),
                            available_cash=cash_remaining,
                            capital_fraction=decision.capital_fraction,
                        )
                        continue
                    amount = min(amount, budget.amount_krw)
                amount = math.floor(amount)
                if amount <= 0 or decision.expected_move_pct <= actual_cost * 2.0:
                    self._entry_status(market, '실제 호가 유동성/왕복 거래비용 조건 미달', amount_krw=amount, cost_pct=actual_cost); continue
                check = self.risk.can_open(available_cash=cash_remaining, amount_krw=amount, min_order_krw=min_bid, stream_age_seconds=max(self.strategy.trade_age(market), self.strategy.book_age(market)))
                if not check.allowed:
                    self._entry_status(market, f'매수 차단: {check.reason}', amount_krw=amount,
                                       min_order_krw=max(min_bid, self.config.safety.min_order_krw),
                                       available_cash=cash_remaining, capital_fraction=decision.capital_fraction)
                    self._emit("risk", f"{market} 매수 차단: {check.reason}", amount_krw=amount,
                               min_order_krw=max(min_bid, self.config.safety.min_order_krw), available_cash=cash_remaining)
                    continue
                # The durable intent is created after this planning pass.  A
                # signal, best-level capacity or book generation can change
                # while storage commits. Re-run the complete entry policy
                # inside the serialized submission gate and require the exact
                # snapshot used by that final calculation to remain current.
                planned_amount = amount
                planned_actual_risk = actual_risk

                def buy_pre_submit_check() -> bool:
                    guard_started_at = time.monotonic()
                    guard_decision = self.strategy.evaluate_entry(
                        market,
                        bid_fee=bid_fee,
                        ask_fee=ask_fee,
                        health=health,
                        regime_factor=regime_factor,
                    )
                    if guard_decision.signal != Signal.BUY:
                        self._entry_status(
                            market,
                            f"POST 직전 매수 재확인 해제: {guard_decision.reason}",
                        )
                        return False
                    guard_book = self._fresh_entry_book(market)
                    if (
                        guard_book is None
                        or self.strategy.trade_age(market)
                        > self.config.safety.market_data_stale_seconds
                    ):
                        self._entry_status(
                            market,
                            "POST 직전 체결/호가가 만료되어 매수 차단",
                        )
                        return False
                    guard_token = self._entry_book_token(guard_book)
                    guard_capacity = self._liquidity_capacity(
                        guard_book,
                        expected_move_pct=guard_decision.expected_move_pct,
                        bid_fee=bid_fee,
                        ask_fee=ask_fee,
                    )
                    guard_buy_slip, guard_buy_fillable = self._simulate_buy_slippage(
                        guard_book, planned_amount
                    )
                    guard_sell_slip, guard_sell_fillable = self._simulate_sell_slippage(
                        guard_book, planned_amount
                    )
                    tolerance = max(1e-6, planned_amount * 1e-12)
                    if (
                        guard_capacity + tolerance < planned_amount
                        or guard_buy_fillable + tolerance < planned_amount
                        or guard_sell_fillable + tolerance < planned_amount
                    ):
                        self._entry_status(
                            market,
                            "POST 직전 최우선 호가 유동성이 줄어 매수 차단",
                            amount_krw=planned_amount,
                            liquidity_capacity_krw=guard_capacity,
                        )
                        return False
                    guard_cost = (
                        bid_fee
                        + ask_fee
                        + guard_book.spread_pct
                        + guard_buy_slip
                        + guard_sell_slip
                    )
                    guard_risk = max(
                        guard_decision.initial_risk_pct, guard_cost * 1.8
                    )
                    if (
                        guard_decision.expected_move_pct <= guard_cost * 2.0
                        or guard_risk > planned_actual_risk + 1e-12
                    ):
                        self._entry_status(
                            market,
                            "POST 직전 거래비용/위험 조건이 악화되어 매수 차단",
                            cost_pct=guard_cost,
                        )
                        return False
                    guard_budget = self.risk.entry_budget(
                        equity_krw=equity,
                        available_cash_krw=cash_remaining,
                        gross_exposure_krw=gross_exposure,
                        open_risk_krw=open_risk,
                        initial_risk_pct=guard_risk,
                        liquidity_capacity_krw=guard_capacity,
                        signal_fraction=guard_decision.capital_fraction,
                        session_return_pct=session_return,
                        min_order_krw=min_bid,
                    )
                    guard_open = self.risk.can_open(
                        available_cash=cash_remaining,
                        amount_krw=planned_amount,
                        min_order_krw=min_bid,
                        stream_age_seconds=max(
                            self.strategy.trade_age(market),
                            max(0.0, time.monotonic() - guard_book.received_at),
                        ),
                    )
                    if (
                        not guard_budget.allowed
                        or guard_budget.amount_krw + tolerance < planned_amount
                        or not guard_open.allowed
                    ):
                        reason = (
                            guard_budget.reason
                            if not guard_budget.allowed
                            or guard_budget.amount_krw + tolerance < planned_amount
                            else guard_open.reason
                        )
                        self._entry_status(
                            market,
                            f"POST 직전 위험예산 재검증 차단: {reason}",
                        )
                        return False
                    if guard_started_at + max(
                        0.0, self.config.safety.order_guard_seconds
                    ) < time.monotonic():
                        self._entry_status(
                            market, "POST 직전 매수 재판단 시간 초과 · 매수 차단"
                        )
                        return False
                    confirmed_book = self._fresh_entry_book(market)
                    if (
                        confirmed_book is None
                        or self._entry_book_token(confirmed_book) != guard_token
                    ):
                        self._entry_status(
                            market,
                            "POST 직전 판단 중 호가 세대가 변경되어 매수 차단 · 다음 주기에 재평가",
                        )
                        return False
                    return True

                self._buy_managed(
                    market=market,
                    amount_krw=amount,
                    decision=decision,
                    bid_fee=bid_fee,
                    actual_round_trip_cost=actual_cost,
                    pre_submit_check=buy_pre_submit_check,
                )
                cash_remaining = max(0.0, cash_remaining - amount * (1.0 + bid_fee))
                gross_exposure += amount
                open_risk += amount * actual_risk

    def _publish_portfolio(self) -> None:
        equity, available_cash, total_cash, positions = self._live_portfolio(); managed = self.storage.managed_markets()
        rows: list[dict[str, Any]] = [{"market": "KRW", "quantity": total_cash, "avg_price": 1.0, "current_price": 1.0, "value": total_cash, "managed": False}]
        for position in positions:
            price = self._latest_prices.get(position.market, position.avg_price)
            rows.append({"market": position.market, "quantity": position.total_quantity, "avg_price": position.avg_price, "current_price": price, "value": position.market_value(price), "managed": position.market in managed})
        start = self._session_start_equity or equity; pnl_pct = (equity / start - 1.0) * 100.0 if start else 0.0
        self.events.put({"type": "portfolio", "equity": equity, "cash": total_cash, "available_cash": available_cash, "pnl_pct": pnl_pct, "positions": rows})
