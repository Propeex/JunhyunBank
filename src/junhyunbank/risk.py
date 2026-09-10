from __future__ import annotations

from dataclasses import dataclass
import math

from .config import SafetyConfig


@dataclass(slots=True)
class RiskCheck:
    allowed: bool
    reason: str


class RiskManager:
    """투자전략의 고정 한도 없이 시스템 안전만 담당한다."""

    def __init__(self, config: SafetyConfig) -> None:
        self.config = config
        self.emergency = False
        self.api_failures = 0

    def reset_session(self) -> None:
        self.emergency = False
        self.api_failures = 0

    def trigger_emergency(self) -> None:
        self.emergency = True

    def report_api_success(self) -> None:
        self.api_failures = 0

    def report_api_failure(self) -> None:
        self.api_failures += 1

    def can_open(self, *, available_cash: float, amount_krw: float, min_order_krw: float, stream_age_seconds: float) -> RiskCheck:
        if not all(math.isfinite(x) and x >= 0 for x in (available_cash, amount_krw, min_order_krw, stream_age_seconds)):
            return RiskCheck(False, '주문 금액/시장 데이터가 유효하지 않음')
        if self.emergency:
            return RiskCheck(False, "긴급 정지 상태")
        if self.api_failures >= self.config.max_api_failures:
            return RiskCheck(False, "API 오류가 연속 발생하여 신규 주문을 차단함")
        if stream_age_seconds > self.config.market_data_stale_seconds:
            return RiskCheck(False, "실시간 시장 데이터가 오래되어 신규 주문을 차단함")
        minimum = max(self.config.min_order_krw, min_order_krw)
        if amount_krw < minimum:
            return RiskCheck(False, "동적 주문금액이 업비트 최소 주문금액보다 작음")
        if available_cash < amount_krw:
            return RiskCheck(False, "주문 가능 KRW 부족")
        return RiskCheck(True, "주문 가능")
