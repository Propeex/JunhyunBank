from __future__ import annotations

from dataclasses import dataclass
import math

from .config import SafetyConfig


@dataclass(slots=True)
class RiskCheck:
    allowed: bool
    reason: str


@dataclass(slots=True)
class EntryBudget:
    allowed: bool
    amount_krw: float
    reason: str
    limiting_factor: str = ""


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

    def entry_budget(
        self,
        *,
        equity_krw: float,
        available_cash_krw: float,
        gross_exposure_krw: float,
        open_risk_krw: float,
        initial_risk_pct: float,
        liquidity_capacity_krw: float,
        signal_fraction: float,
        session_return_pct: float,
        min_order_krw: float,
    ) -> EntryBudget:
        """Return a loss-risk-based order budget, not a fixed KRW cap.

        ``initial_risk_pct`` is the strategy's immutable emergency distance.
        Multiplying it by notional estimates the planned loss at that stop. The
        estimate is not a guarantee (gaps and API outages can be worse), so cash,
        gross exposure and displayed-liquidity participation are independent
        brakes rather than substitutes.
        """
        values = (
            equity_krw,
            available_cash_krw,
            gross_exposure_krw,
            open_risk_krw,
            initial_risk_pct,
            liquidity_capacity_krw,
            signal_fraction,
            session_return_pct,
            min_order_krw,
        )
        if not all(math.isfinite(float(value)) for value in values):
            return EntryBudget(False, 0.0, "위험예산 입력값이 유효하지 않음")
        if (
            equity_krw <= 0
            or available_cash_krw < 0
            or gross_exposure_krw < 0
            or open_risk_krw < 0
            or initial_risk_pct <= 0
            or liquidity_capacity_krw < 0
            or min_order_krw < 0
        ):
            return EntryBudget(False, 0.0, "위험예산 입력값이 유효하지 않음")

        drawdown_limit = max(
            0.0, float(getattr(self.config, "session_drawdown_halt_fraction", 0.02))
        )
        if drawdown_limit > 0 and session_return_pct <= -drawdown_limit:
            return EntryBudget(
                False,
                0.0,
                f"세션 손실 {session_return_pct:.2%}로 신규매수 중단",
                "session_drawdown",
            )

        reserve = _fraction(
            getattr(self.config, "cash_reserve_fraction", 0.15)
        )
        single_position = _fraction(
            getattr(self.config, "single_position_fraction", 0.15)
        )
        gross_limit = _fraction(
            getattr(self.config, "gross_exposure_fraction", 0.60)
        )
        per_trade_risk = _fraction(
            getattr(self.config, "per_trade_risk_fraction", 0.0025)
        )
        portfolio_risk = _fraction(
            getattr(self.config, "portfolio_risk_fraction", 0.01)
        )
        participation = _fraction(
            getattr(self.config, "liquidity_participation_fraction", 0.10)
        )
        signal = _fraction(signal_fraction)

        candidates = {
            "signal": available_cash_krw * signal,
            "cash_reserve": max(0.0, available_cash_krw - equity_krw * reserve),
            "single_position": equity_krw * single_position,
            "gross_exposure": max(0.0, equity_krw * gross_limit - gross_exposure_krw),
            "per_trade_risk": equity_krw * per_trade_risk / initial_risk_pct,
            "portfolio_risk": max(
                0.0, equity_krw * portfolio_risk - open_risk_krw
            ) / initial_risk_pct,
            "liquidity": liquidity_capacity_krw * participation,
        }
        limiting_factor, amount = min(candidates.items(), key=lambda item: item[1])
        amount = max(0.0, float(amount))
        minimum = max(self.config.min_order_krw, min_order_krw)
        if amount < minimum:
            labels = {
                "signal": "신호 강도",
                "cash_reserve": "현금 예비금",
                "single_position": "단일 포지션 노출",
                "gross_exposure": "총 자산 노출",
                "per_trade_risk": "거래당 손실위험",
                "portfolio_risk": "포트폴리오 손실위험",
                "liquidity": "표시 유동성 참여율",
            }
            return EntryBudget(
                False,
                amount,
                f"{labels.get(limiting_factor, limiting_factor)} 예산이 최소 주문금액보다 작음",
                limiting_factor,
            )
        return EntryBudget(True, amount, "위험예산 내 주문 가능", limiting_factor)


def _fraction(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))
