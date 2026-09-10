from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(slots=True)
class RiskConfig:
    order_krw: float = 10_000.0
    max_position_count: int = 1
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    daily_loss_limit_pct: float = 0.03
    min_order_krw: float = 5_000.0
    fee_rate: float = 0.0005


@dataclass(slots=True)
class StrategyConfig:
    candidate_count: int = 8
    candle_unit: int = 5
    candle_count: int = 120
    fast_ma: int = 10
    slow_ma: int = 30
    rsi_period: int = 14
    rsi_entry_min: float = 50.0
    rsi_entry_max: float = 68.0
    rsi_exit: float = 75.0
    min_24h_trade_value: float = 2_000_000_000.0
    candidate_refresh_seconds: int = 60
    evaluation_seconds: int = 15


@dataclass(slots=True)
class AppConfig:
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    paper_starting_cash: float = 1_000_000.0
