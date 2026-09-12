from __future__ import annotations

import math
import queue
import threading
import time
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


class TradingEngine(OrderExecution):
    def __init__(self, client: UpbitClient, config: AppConfig | None = None, storage: Storage | None = None) -> None:
        self.client = client
        self.config = config or AppConfig()
        self.storage = storage or Storage()
        self.strategy = MicroFlowStrategy(self.config.strategy)
        self.risk = RiskManager(self.config.safety)
        self.health_governor = StrategyHealthGovernor(minimum_samples=max(8, self.config.strategy.health_lookback // 5))
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._hard_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = EngineState.STOPPED
        self._global_streams: list[MarketStream] = []
        self._deep_stream: MarketStream | None = None
        self._allowed_markets: list[str] = []
        self._deep_markets: list[str] = []
        self._deep_entered_at: dict[str, float] = {}
        self._latest_prices: dict[str, float] = {}
        self._last_price_ui_emit: dict[str, float] = {}
        self._scan_seconds = self._evaluation_seconds = 0.0
        self._last_runtime_sample = 0.0
        self._order_lock = threading.Lock()
        self._fee_cache: dict[str, tuple[float, float, float, float, float]] = {}
        self._session_start_equity: float | None = None
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
        return self._state == EngineState.RUNNING and not self.risk.emergency

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
        self._entry_messages.clear()
        self._fee_cache.clear()
        self._last_price_ui_emit.clear()
        self._scan_seconds = self._evaluation_seconds = 0.0
        self._last_runtime_sample = 0.0
        self._market_discovery_retry_at = 0.0
        self._market_discovery_ready = False
        self.risk.reset_session()
        self._hard_stop.clear()
        self._update_shutdown = False
        self._run_started_at = time.monotonic()
        self._last_no_candidate_warning = 0.0
        equity, _, _, _ = self._live_portfolio()
        self._session_start_equity = equity
        self._state = EngineState.RUNNING
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()
        self._emit("status", f"LIVE 자동매매 시작 · V{__version__}", version=__version__)

    def request_stop(self) -> None:
        if not self.running:
            self._state = EngineState.STOPPED
            self._emit("status", "이미 자동매매가 종료되어 있습니다.")
            return
        if self._state == EngineState.DRAINING:
            return
        self._state = EngineState.DRAINING
        self._emit("status", "종료 대기: 신규 매수를 중단하고 JunhyunBank 보유 포지션의 전략 청산을 기다립니다.")

    def emergency_stop(self) -> None:
        self.risk.trigger_emergency()
        self._state = EngineState.STOPPED
        self._hard_stop.set()
        self._emit("emergency", "긴급 정지: 신규 주문과 전략 실행을 즉시 중단했습니다. 보유 포지션은 강제 청산하지 않습니다.")

    def shutdown_for_update(self) -> None:
        self._update_shutdown = True
        self._state = EngineState.STOPPED
        self._hard_stop.set()
        self._emit("status", "업데이트를 위해 엔진을 일시 정지합니다. 관리 포지션은 재시작 후 이어서 관리합니다.")

    def drain_events(self, limit: int = 200) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
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
            )
            self._deep_stream.start()
        added = sorted(set(markets) - previous)
        removed = sorted(previous - set(markets))
        if added or removed:
            self.events.put({"type": "deep_set", "added": added, "removed": removed, "count": len(markets)})

    def _on_trade(self, event: dict[str, Any]) -> None:
        self.strategy.on_trade(event)
        market = str(event.get("code") or "")
        price = float(event.get("trade_price") or 0.0)
        if not market or price <= 0:
            return
        self._latest_prices[market] = price
        now = time.monotonic()
        if now - self._last_price_ui_emit.get(market, 0.0) >= 0.25:
            self._last_price_ui_emit[market] = now
            self.events.put({"type": "price", "market": market, "price": price,
                             "timestamp": float(event.get("trade_timestamp") or event.get("timestamp") or time.time() * 1000) / 1000.0})

    def _on_orderbook(self, event: dict[str, Any]) -> None:
        self.strategy.on_orderbook(event)

    @staticmethod
    def _finite_age(value: float) -> float | None:
        return value if math.isfinite(value) else None

    def _publish_runtime_health(self, ranked: list[tuple[str, float]]) -> None:
        tracked = sum(1 for market in self._allowed_markets if self.strategy.latest_price(market))
        warmed = sum(
            1
            for market in self._allowed_markets
            if self.strategy.warmup_ratio(market) >= 1.0 and self.strategy.trade_age(market) <= 30.0
        )
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
                       deep_connected=deep_connected, warmed_markets=warmed)

    def _run(self) -> None:
        next_market_refresh = next_candidate_refresh = next_evaluation = next_portfolio = next_runtime_health = 0.0
        last_ranked: list[tuple[str, float]] = []
        try:
            while not self._hard_stop.is_set():
                now = time.monotonic()
                if now >= next_market_refresh or not self._allowed_markets:
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
                    self._evaluation_seconds = time.monotonic() - evaluation_started
                    next_evaluation = now + self.config.strategy.evaluation_seconds
                if now >= next_portfolio:
                    try:
                        self._publish_portfolio()
                    except Exception as exc:
                        self._emit("warning", f"자산 갱신 실패: {exc}")
                    next_portfolio = now + self.config.strategy.portfolio_publish_seconds
                if self._state == EngineState.DRAINING and not self.storage.managed_markets() and not self.storage.pending_orders():
                    self._emit("drain_complete", "관리 포지션 청산이 완료되어 자동매매를 종료합니다.")
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
            self._fee_cache[market] = (now, bid_fee, ask_fee, min_bid, min_ask)
            self.risk.report_api_success()
            return bid_fee, ask_fee, min_bid, min_ask
        except Exception as exc:
            self.risk.report_api_failure(); self._emit("warning", f"{market} 주문 가능정보 조회 실패: {exc}"); return None

    def _live_portfolio(self) -> tuple[float, float, float, list[Position]]:
        accounts = self.client.get_accounts(); self.risk.report_api_success()
        available_cash = total_cash = 0.0; positions: list[Position] = []
        for row in accounts:
            currency = str(row.get("currency", "")); balance = float(row.get("balance") or 0.0); locked = float(row.get("locked") or 0.0)
            if currency == "KRW":
                available_cash, total_cash = balance, balance + locked; continue
            if balance + locked <= 0 or str(row.get("unit_currency") or "KRW") != "KRW":
                continue
            positions.append(Position(f"KRW-{currency}", balance, float(row.get("avg_buy_price") or 0.0), locked))
        missing = [p.market for p in positions if not self._latest_prices.get(p.market)]
        for chunk in self._chunks(missing, 100):
            for row in self.client.get_tickers(chunk):
                market, price = str(row.get("market") or ""), float(row.get("trade_price") or 0.0)
                if market and price > 0:
                    self._latest_prices[market] = price
        equity = total_cash + sum(p.market_value(self._latest_prices.get(p.market, p.avg_price)) for p in positions)
        return equity, available_cash, total_cash, positions

    @staticmethod
    def _created_elapsed(state: dict[str, Any] | None) -> float:
        try:
            created = datetime.fromisoformat(str((state or {}).get("created_at") or ""))
            return max(0.0, (datetime.now() - created).total_seconds())
        except Exception:
            return 0.0

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

    def _evaluate_cycle(self) -> None:
        self._reconcile_orders()
        pending = self.storage.pending_orders()
        pending_markets = {o['market'] for o in pending}
        try:
            _, available_cash, _, positions = self._live_portfolio()
        except Exception as exc:
            self.risk.report_api_failure(); self._entry_status("전체", f"잔고 조회 실패 · 자동 재시도: {exc}"); return
        positions_by_market = {p.market: p for p in positions}; managed = self.storage.managed_markets()
        with self._order_lock:
            for market in sorted(managed):
                if market in pending_markets: continue
                if self._hard_stop.is_set() or self.risk.emergency: return
                state = self.storage.get_managed_state(market); position = positions_by_market.get(market); elapsed = self._created_elapsed(state)
                if position is None or position.total_quantity <= 0:
                    if elapsed > self.config.safety.settlement_grace_seconds:
                        self.storage.unmark_managed_position(market); self._emit("warning", f"{market} 관리표시는 있으나 잔고가 없어 관리상태를 해제했습니다.")
                    continue
                managed_qty = float((state or {}).get("managed_quantity") or 0.0)
                if managed_qty <= 0:
                    self._entry_status(market, "관리 수량 미확인 · 계정 잔고를 자동관리 수량으로 추정하지 않습니다."); continue
                current_price = self._latest_prices.get(market) or self.strategy.latest_price(market)
                if not current_price: continue
                entry_price = float((state or {}).get("entry_price") or position.avg_price)
                peak = max(float((state or {}).get("peak_price") or entry_price), current_price); self.storage.update_managed_peak(market, peak)
                fee_info = self._fee_info(market)
                if fee_info: _, ask_fee, _, min_ask = fee_info
                else: ask_fee, min_ask = float((state or {}).get("entry_fee_rate") or 0.0005), self.config.safety.min_order_krw
                entry_fee = float((state or {}).get("entry_fee_rate") or ask_fee); book = self.strategy.book(market); spread = book.spread_pct if book else 0.0
                round_trip = entry_fee + ask_fee + spread * 1.5
                initial_risk = float((state or {}).get("initial_risk_pct") or 0.0)
                if initial_risk <= 0:
                    initial_risk = self.strategy.suggest_emergency_risk(market, round_trip)
                    if initial_risk > 0: self.storage.mark_managed_position(market, initial_risk_pct=initial_risk)
                horizon = float((state or {}).get("expected_horizon_seconds") or 300.0)
                decision = self.strategy.evaluate_position(market, entry_price=entry_price, current_price=current_price, peak_price=peak, initial_risk_pct=initial_risk, round_trip_cost_pct=round_trip, elapsed_seconds=elapsed, expected_horizon_seconds=horizon)
                self.events.put({'type': 'position_diagnostic', 'market': market, 'reason': decision.reason,
                                 'score': decision.score, 'elapsed': elapsed})
                now = time.monotonic()
                key = f'position:{market}'
                previous, logged_at = self._entry_messages.get(key, ('', -60.0))
                if now - logged_at >= 60.0 or (previous != decision.reason and now - logged_at >= 10.0) or decision.signal == Signal.SELL:
                    self._entry_messages[key] = (decision.reason, now)
                    self._emit('position_state', f'{market}: {decision.reason}', elapsed_seconds=elapsed, hold_quality=decision.hold_quality)
                if decision.signal == Signal.SELL:
                    self._sell_managed(market=market, position=position, managed_qty=managed_qty, current_price=current_price, min_ask_krw=min_ask, ask_fee=ask_fee, state=state or {}, reason=decision.reason)
            if self._state != EngineState.RUNNING or self.risk.emergency: return
            health = self.health_governor.score(self.storage.strategy_outcomes(self.config.strategy.health_lookback)); regime_name, regime_factor = self.strategy.market_regime(self._allowed_markets)
            self.events.put({"type": "strategy_health", "health": health, "regime": regime_name})
            if pending:
                self._entry_status('전체', f'미확정 주문 {len(pending)}건 확인 중 · 신규 매수 차단'); return
            if not getattr(self, '_market_discovery_ready', True):
                self._entry_status('전체', '시장 경보 목록 갱신 실패 · 재확인 전 신규 매수 차단'); return
            if health <= 0:
                self._entry_status('전체', '전략 건강도 0 · 최근 실거래 손실로 신규 매수 중단 · 성과 검토 필요'); return
            if regime_factor <= 0:
                self._entry_status('전체', '시장 PANIC · 신규 매수 차단'); return
            if not self._deep_markets:
                self._entry_status('전체', '진입 후보 대기 · 시세 연결/3분 워밍업 상태를 확인하세요.'); return
            self._entry_status('전체', '진입 조건 평가 중 · 아래 종목별 대기 이유를 확인하세요.')
            held = set(positions_by_market); decisions: list[tuple[str, Any, tuple[float, float, float, float]]] = []
            for market, _ in self.strategy.rank_markets(self._deep_markets, self.config.strategy.deep_candidate_count):
                if market not in self._allowed_markets: continue
                if market in held or market in managed:
                    self._entry_status(market, '이미 보유 중 · 추가 매수 제외'); continue
                if self._state != EngineState.RUNNING or self.risk.emergency: return
                if max(self.strategy.trade_age(market), self.strategy.book_age(market)) > self.config.safety.market_data_stale_seconds:
                    self._entry_status(market, '체결/호가 수신 대기 또는 3초 이상 지연',
                                       trade_age=self._finite_age(self.strategy.trade_age(market)),
                                       book_age=self._finite_age(self.strategy.book_age(market))); continue
                fee_info = self._fee_info(market)
                if not fee_info: continue
                bid_fee, ask_fee, _, _ = fee_info
                decision = self.strategy.evaluate_entry(market, bid_fee=bid_fee, ask_fee=ask_fee, health=health, regime_factor=regime_factor)
                self._entry_status(market, decision.reason, score=decision.score,
                                   expected_move_pct=decision.expected_move_pct, cost_pct=decision.round_trip_cost_pct)
                if decision.signal == Signal.BUY: decisions.append((market, decision, fee_info))
            decisions.sort(key=lambda item: item[1].score, reverse=True); cash_remaining = available_cash
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
                desired = cash_remaining * decision.capital_fraction
                capacity = self._liquidity_capacity(book, expected_move_pct=decision.expected_move_pct, bid_fee=bid_fee, ask_fee=ask_fee)
                amount = min(desired, capacity, cash_remaining / (1.0 + bid_fee))
                buy_slip, fillable = self._simulate_buy_slippage(book, amount); sell_slip, _ = self._simulate_sell_slippage(book, amount); amount = min(amount, fillable)
                actual_cost = bid_fee + ask_fee + book.spread_pct + buy_slip + sell_slip
                amount = math.floor(amount)
                if amount <= 0 or decision.expected_move_pct <= actual_cost * 2.0:
                    self._entry_status(market, '실제 호가 유동성/왕복 거래비용 조건 미달', amount_krw=amount, cost_pct=actual_cost); continue
                check = self.risk.can_open(available_cash=cash_remaining, amount_krw=amount, min_order_krw=min_bid, stream_age_seconds=max(self.strategy.trade_age(market), self.strategy.book_age(market)))
                if not check.allowed:
                    self._emit("risk", f"{market} 매수 차단: {check.reason}"); continue
                self._buy_managed(market=market, amount_krw=amount, decision=decision, bid_fee=bid_fee, actual_round_trip_cost=actual_cost)
                cash_remaining = max(0.0, cash_remaining - amount * (1.0 + bid_fee))

    def _publish_portfolio(self) -> None:
        equity, available_cash, total_cash, positions = self._live_portfolio(); managed = self.storage.managed_markets()
        rows: list[dict[str, Any]] = [{"market": "KRW", "quantity": total_cash, "avg_price": 1.0, "current_price": 1.0, "value": total_cash, "managed": False}]
        for position in positions:
            price = self._latest_prices.get(position.market, position.avg_price)
            rows.append({"market": position.market, "quantity": position.total_quantity, "avg_price": position.avg_price, "current_price": price, "value": position.market_value(price), "managed": position.market in managed})
        start = self._session_start_equity or equity; pnl_pct = (equity / start - 1.0) * 100.0 if start else 0.0
        self.events.put({"type": "portfolio", "equity": equity, "cash": total_cash, "available_cash": available_cash, "pnl_pct": pnl_pct, "positions": rows})
