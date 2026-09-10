"""Persistent order submission and terminal-fill reconciliation."""
import math
import time
from typing import Any
import uuid

from .models import EngineState, Position
from .upbit import UpbitAPIError


class OrderExecution:
    def _reconcile_orders(self) -> None:
        for intent in self.storage.pending_orders():
            if self._hard_stop.is_set():
                return
            try:
                detail = self.client.get_order(identifier=intent['identifier'])
                if self.storage.complete_order_intent(intent['identifier'], detail):
                    self.risk.report_api_success()
                    if intent['side'] == 'SELL' and intent['market'] not in self.storage.managed_markets():
                        self.strategy.notify_exit(intent['market'])
                    self._emit('trade', f"{intent['market']} {intent['side']} 주문 확인 완료 · 체결수량 {detail.get('executed_volume', '0')}")
            except Exception as exc:
                # A timeout or not-found immediately after POST cannot prove that
                # the exchange rejected the order. Keep the durable intent.
                self._entry_status(intent['market'], f'주문 결과 확인 대기: {exc}')

    def _submit_order(self, market: str, side: str, context: dict, submit) -> dict | None:
        if self._hard_stop.is_set() or self.risk.emergency:
            return None
        if side == 'BUY' and not self.accepting_entries:
            return None
        retries = getattr(self, '_order_retry_at', {})
        if time.monotonic() < retries.get(market, 0):
            self._entry_status(market, '최근 주문 오류 후 재시도 대기')
            return None
        if any(o['market'] == market for o in self.storage.pending_orders()):
            return None
        identifier = f'junhyunbank-{uuid.uuid4()}'
        self.storage.create_order_intent(identifier, market, side, context)
        # Stop can arrive while the durable intent is being committed.
        if self._hard_stop.is_set() or self.risk.emergency or (side == 'BUY' and not self.accepting_entries):
            self.storage.reject_order_intent(identifier, '제출 전 중지')
            return None
        try:
            accepted = submit(identifier)
            self.risk.report_api_success()
        except Exception as exc:
            self.risk.report_api_failure()
            self._order_retry_at = {**retries, market: time.monotonic()+60.0}
            if isinstance(exc, UpbitAPIError) and exc.status_code is not None and 400 <= exc.status_code < 500:
                self.storage.reject_order_intent(identifier, str(exc))
            self._emit('error', f'{market} {side} 주문 응답 오류: {exc} · 불확실한 주문은 재전송하지 않고 조회합니다.')
            return None
        detail = self._settle_order(accepted)
        if self.storage.complete_order_intent(identifier, detail):
            self._emit('trade', f"[LIVE] {market} {side} 확인 · 체결수량 {detail.get('executed_volume', '0')}")
            return detail
        self._entry_status(market, '주문 체결 확인 대기 · 중복 주문 차단')
        return None

    def _buy_managed(self, *, market: str, amount_krw: float, decision: Any, bid_fee: float, actual_round_trip_cost: float) -> None:
        if not self.accepting_entries or market not in self._allowed_markets or not getattr(self, '_market_discovery_ready', True):
            return
        if max(self.strategy.trade_age(market), self.strategy.book_age(market)) > self.config.safety.market_data_stale_seconds:
            self._entry_status(market, '주문 직전 시세 지연으로 매수 차단')
            return
        if not math.isfinite(amount_krw) or amount_krw < self.config.safety.min_order_krw:
            return
        context = dict(reason=decision.reason, initial_risk_pct=max(decision.initial_risk_pct, actual_round_trip_cost*1.8), entry_score=decision.score,
                       signal_kind=decision.kind.value, expected_horizon_seconds=decision.expected_horizon_seconds)
        self._submit_order(market, 'BUY', context, lambda identifier: self.client.place_best_ioc_buy(market, math.floor(amount_krw), identifier=identifier))

    def _sell_managed(self, *, market: str, position: Position, managed_qty: float, current_price: float, min_ask_krw: float, ask_fee: float, state: dict, reason: str) -> None:
        available = min(max(0, managed_qty), max(0, position.quantity))
        if available <= 0:
            return
        if available*current_price < min_ask_krw:
            self._entry_status(market, '관리 잔여수량이 최소 매도금액 미만 · 수량 보존')
            return
        context = dict(reason=reason, min_ask_krw=min_ask_krw)
        detail = self._submit_order(market, 'SELL', context, lambda identifier: self.client.place_best_ioc_sell(market, available, identifier=identifier))
        # Market fallback only after an explicitly terminal zero-fill IOC.
        if detail and float(detail.get('executed_volume') or 0) == 0 and reason.startswith('Emergency Stop'):
            self._submit_order(market, 'SELL', context, lambda identifier: self.client.place_market_sell(market, available, identifier=identifier))
        if market not in self.storage.managed_markets():
            self.strategy.notify_exit(market)
