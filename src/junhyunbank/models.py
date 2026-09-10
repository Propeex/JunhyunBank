from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class SignalKind(str, Enum):
    NONE = "NONE"
    IGNITION = "IGNITION"
    PULLBACK = "PULLBACK"


class EngineState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"


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
    kind: SignalKind = SignalKind.NONE
    expected_move_pct: float = 0.0
    round_trip_cost_pct: float = 0.0
    initial_risk_pct: float = 0.0
    capital_fraction: float = 0.0
    expected_horizon_seconds: float = 0.0
    hold_quality: float = 0.0


@dataclass(slots=True)
class Position:
    market: str
    quantity: float
    avg_price: float
    locked: float = 0.0

    @property
    def total_quantity(self) -> float:
        return self.quantity + self.locked

    def market_value(self, current_price: float) -> float:
        return self.total_quantity * current_price
