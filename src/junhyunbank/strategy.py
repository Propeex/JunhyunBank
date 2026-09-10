from __future__ import annotations

from abc import ABC, abstractmethod
from statistics import fmean

from .config import StrategyConfig
from .models import Candle, Signal, StrategyDecision


class Strategy(ABC):
    @abstractmethod
    def evaluate(self, candles: list[Candle]) -> StrategyDecision:
        raise NotImplementedError


def _rsi(closes: list[float], period: int) -> float:
    if len(closes) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    recent = closes[-(period + 1):]
    for previous, current in zip(recent, recent[1:]):
        delta = current - previous
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class TrendRSIStrategy(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def evaluate(self, candles: list[Candle]) -> StrategyDecision:
        required = max(self.config.slow_ma + 2, self.config.rsi_period + 2)
        if len(candles) < required:
            return StrategyDecision(Signal.HOLD, 0.0, "캔들 데이터 부족")

        closes = [c.close for c in candles]
        fast = fmean(closes[-self.config.fast_ma:])
        slow = fmean(closes[-self.config.slow_ma:])
        previous_fast = fmean(closes[-self.config.fast_ma - 1:-1])
        rsi = _rsi(closes, self.config.rsi_period)
        price = closes[-1]

        trend_pct = (fast / slow - 1.0) * 100.0 if slow else 0.0
        slope_pct = (fast / previous_fast - 1.0) * 100.0 if previous_fast else 0.0

        if fast < slow or rsi >= self.config.rsi_exit:
            return StrategyDecision(
                Signal.SELL,
                max(abs(trend_pct), max(0.0, rsi - self.config.rsi_exit)),
                f"추세 약화 또는 과열: MA={trend_pct:.2f}%, RSI={rsi:.1f}",
            )

        entry = (
            fast > slow
            and fast > previous_fast
            and price >= fast
            and self.config.rsi_entry_min <= rsi <= self.config.rsi_entry_max
        )
        if entry:
            score = max(0.0, trend_pct) * 3.0 + max(0.0, slope_pct) * 2.0
            score += max(0.0, 10.0 - abs(58.0 - rsi)) / 10.0
            return StrategyDecision(
                Signal.BUY,
                score,
                f"상승 추세: MA={trend_pct:.2f}%, slope={slope_pct:.2f}%, RSI={rsi:.1f}",
            )

        return StrategyDecision(
            Signal.HOLD,
            0.0,
            f"진입 조건 대기: MA={trend_pct:.2f}%, RSI={rsi:.1f}",
        )
