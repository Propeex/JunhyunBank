from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig


@dataclass(slots=True)
class RiskCheck:
    allowed: bool
    reason: str


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.emergency = False
        self.daily_start_equity: float | None = None

    def reset_session(self, equity: float) -> None:
        self.emergency = False
        self.daily_start_equity = max(0.0, equity)

    def trigger_emergency(self) -> None:
        self.emergency = True

    def daily_loss_pct(self, current_equity: float) -> float:
        if not self.daily_start_equity:
            return 0.0
        return max(0.0, 1.0 - current_equity / self.daily_start_equity)

    def can_open(
        self,
        *,
        current_equity: float,
        available_cash: float,
        open_position_count: int,
    ) -> RiskCheck:
        if self.emergency:
            return RiskCheck(False, "긴급 정지 상태")
        if self.daily_loss_pct(current_equity) >= self.config.daily_loss_limit_pct:
            return RiskCheck(False, "일일 최대 손실 한도 도달")
        if open_position_count >= self.config.max_position_count:
            return RiskCheck(False, "최대 보유 포지션 수 도달")
        amount = self.config.order_krw
        if amount < self.config.min_order_krw:
            return RiskCheck(False, "설정 주문금액이 최소 주문금액보다 작음")
        if available_cash < amount * (1.0 + self.config.fee_rate):
            return RiskCheck(False, "주문 가능 KRW 부족")
        return RiskCheck(True, "주문 가능")

    def exit_reason(self, *, avg_price: float, current_price: float) -> str | None:
        if avg_price <= 0 or current_price <= 0:
            return None
        pnl = current_price / avg_price - 1.0
        if pnl <= -self.config.stop_loss_pct:
            return f"손절 {pnl * 100:.2f}%"
        if pnl >= self.config.take_profit_pct:
            return f"익절 {pnl * 100:.2f}%"
        return None
