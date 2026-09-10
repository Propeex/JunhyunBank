from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(slots=True)
class Candle:
    market: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_value: float


@dataclass(slots=True)
class StrategyDecision:
    signal: Signal
    score: float
    reason: str


@dataclass(slots=True)
class Position:
    market: str
    quantity: float
    avg_price: float

    def market_value(self, current_price: float) -> float:
        return self.quantity * current_price
