"""Persistent order submission and terminal-fill reconciliation."""
import math
import time
from typing import Any
import uuid

from .models import Position
from .upbit import UpbitAPIError


class OrderExecution:
    def _submission_allowed(self, market: str, side: str) -> bool:
        if self._hard_stop.is_set() or self.risk.emergency:
            return False
        if side != 'BUY':
            state = self.storage.get_managed_state(market)
            return bool(
                state is not None
                and str(state.get('status') or 'ACTIVE').upper()
                != 'QUARANTINED'
            )
        if (
            not self.accepting_entries
            or market not in self._allowed_markets
            or not getattr(self, '_market_discovery_ready', True)
        ):
            return False
        global_ready = getattr(self, '_global_market_data_ready', None)
        if callable(global_ready) and not global_ready():
            return False
        fresh = max(
            self.strategy.trade_age(market), self.strategy.book_age(market)
        ) <= self.config.safety.market_data_stale_seconds
        # Recheck stop/state after potentially blocking or instrumented
        # freshness accessors. The submission gate supplies the actual
        # cross-thread linearization; this also protects unusual test hooks.
        return bool(
            fresh
            and not self._hard_stop.is_set()
            and not self.risk.emergency
            and self.accepting_entries
        )

    def _reconcile_orders(self) -> None:
        for intent in self.storage.pending_orders():
            if self._hard_stop.is_set():
                return
            if intent.get('submitted_at') is None and intent.get('intent_version') == 1:
                if self.storage.reject_prepared_order_intent(
                    intent['identifier'],
                    '재시작 시 POST 이전 PREPARED 주문으로 확인되어 안전 폐기',
                ):
                    self._entry_status(
                        intent['market'],
                        'POST 이전에 중단된 주문 의도를 안전 폐기 · 거래소 주문 없음',
                    )
                continue
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

    def _submit_order(
        self,
        market: str,
        side: str,
        context: dict,
        submit,
        *,
        pre_submit_check=None,
    ) -> dict | None:
        if not self._submission_allowed(market, side):
            return None
        retries = getattr(self, '_order_retry_at', {})
        retry_key = (market, side)
        if time.monotonic() < retries.get(retry_key, 0):
            self._entry_status(market, '최근 주문 오류 후 재시도 대기')
            return None
        if any(o['market'] == market for o in self.storage.pending_orders()):
            return None
        identifier = f'junhyunbank-{uuid.uuid4()}'
        self.storage.create_order_intent(identifier, market, side, context)
        # Stop can arrive while the durable intent is being committed. Hold the
        # submission gate through the POST response so a stop is linearized
        # either before this order (local rejection) or after it (in-flight
        # reconciliation), never inside an unguarded check/POST gap.
        with self._submission_gate:
            if not self._submission_allowed(market, side):
                self.storage.reject_order_intent(identifier, '제출 전 중지/시세 무결성 차단')
                return None
            try:
                self.storage.mark_order_submitted(identifier)
                # Storage hooks and another thread can request a stop while the
                # phase marker is committed. No POST has happened yet, so this
                # intent is still safe to reject explicitly.
                if not self._submission_allowed(market, side):
                    self.storage.reject_order_intent(identifier, 'POST 직전 중지/시세 무결성 차단')
                    return None
                if pre_submit_check is not None:
                    try:
                        locally_allowed = bool(pre_submit_check())
                    except Exception as exc:
                        locally_allowed = False
                        self._entry_status(
                            market,
                            f'POST 직전 주문 정책 재검증 실패 · 주문 보류: {exc}',
                        )
                    if not locally_allowed:
                        self.storage.reject_order_intent(
                            identifier, 'POST 직전 주문 정책 재검증 차단'
                        )
                        return None
                accepted = submit(identifier)
                exchange_order_id = str((accepted or {}).get('uuid') or '')
                if exchange_order_id:
                    self.storage.mark_order_accepted(identifier, exchange_order_id)
                self.risk.report_api_success()
            except Exception as exc:
                self.risk.report_api_failure()
                self._order_retry_at = {**retries, retry_key: time.monotonic()+(60.0 if side == 'BUY' else 1.0)}
                if isinstance(exc, UpbitAPIError) and exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 408:
                    self.storage.reject_order_intent(identifier, str(exc))
                self._emit('error', f'{market} {side} 주문 응답 오류: {exc} · 불확실한 주문은 재전송하지 않고 조회합니다.')
                return None
        detail = self._settle_order(accepted)
        if self.storage.complete_order_intent(identifier, detail):
            self._emit('trade', f"[LIVE] {market} {side} 확인 · 체결수량 {detail.get('executed_volume', '0')}")
            return detail
        self._entry_status(market, '주문 체결 확인 대기 · 중복 주문 차단')
        return None

    def _buy_managed(
        self,
        *,
        market: str,
        amount_krw: float,
        decision: Any,
        bid_fee: float,
        actual_round_trip_cost: float,
        pre_submit_check=None,
    ) -> None:
        if not self.accepting_entries or market not in self._allowed_markets or not getattr(self, '_market_discovery_ready', True):
            return
        if max(self.strategy.trade_age(market), self.strategy.book_age(market)) > self.config.safety.market_data_stale_seconds:
            self._entry_status(market, '주문 직전 시세 지연으로 매수 차단')
            return
        if not math.isfinite(amount_krw) or amount_krw < self.config.safety.min_order_krw:
            return
        context = dict(reason=decision.reason, initial_risk_pct=max(decision.initial_risk_pct, actual_round_trip_cost*1.8), entry_score=decision.score,
                       signal_kind=decision.kind.value, expected_horizon_seconds=decision.expected_horizon_seconds)
        self._submit_order(
            market,
            'BUY',
            context,
            lambda identifier: self.client.place_best_ioc_buy(
                market, math.floor(amount_krw), identifier=identifier
            ),
            pre_submit_check=pre_submit_check,
        )

    def _sell_managed(
        self,
        *,
        market: str,
        position: Position,
        managed_qty: float,
        current_price: float,
        min_ask_krw: float,
        ask_fee: float,
        state: dict,
        reason: str,
        pre_submit_check=None,
    ) -> None:
        quantity_tolerance = max(1e-12, max(0.0, managed_qty) * 1e-10)
        if position.total_quantity + quantity_tolerance < managed_qty:
            # A manual sell/withdrawal can leave a smaller same-asset balance.
            # Its remaining units cannot safely be assumed to be this bot's
            # lot, so selling min(managed, account) could consume user assets.
            self.storage.quarantine_managed_position(market)
            self._entry_status(
                market,
                '계정 총수량이 영속 관리수량보다 작아 소유권 불명 · 격리 후 자동매도 금지',
            )
            return
        available = min(max(0, managed_qty), max(0, position.quantity))
        if available <= 0:
            return
        managed_value = max(0, managed_qty) * current_price
        if managed_value < min_ask_krw:
            self.storage.mark_managed_dust(market)
            self._entry_status(market, '관리 잔여수량이 최소 매도금액 미만 · DUST로 수량/원가 보존')
            return
        self.storage.mark_managed_tradeable(market)
        if available * current_price < min_ask_krw:
            # A temporarily locked balance is not economic dust. Excluding it
            # from DRAINING could finish shutdown while an orderable managed
            # position still exists, so keep it ACTIVE and wait for unlock.
            self._entry_status(
                market,
                '주문 가능 수량은 최소 매도금액 미만이지만 잠긴 관리수량이 있어 DUST로 처리하지 않음',
            )
            return
        context = dict(reason=reason, min_ask_krw=min_ask_krw)
        detail = self._submit_order(
            market,
            'SELL',
            context,
            lambda identifier: self.client.place_best_ioc_sell(
                market, available, identifier=identifier
            ),
            pre_submit_check=pre_submit_check,
        )
        # Market fallback only after an explicitly terminal zero-fill IOC.
        if detail and float(detail.get('executed_volume') or 0) == 0 and reason.startswith('Emergency Stop'):
            self._submit_order(
                market,
                'SELL',
                context,
                lambda identifier: self.client.place_market_sell(
                    market, available, identifier=identifier
                ),
                pre_submit_check=pre_submit_check,
            )
        if market not in self.storage.managed_markets():
            self.strategy.notify_exit(market)
