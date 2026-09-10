from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SafetyConfig:
    min_order_krw: float = 5_000.0
    market_data_stale_seconds: float = 3.0
    order_guard_seconds: float = 0.8
    max_api_failures: int = 5
    settlement_grace_seconds: float = 8.0


@dataclass(slots=True)
class StrategyConfig:
    baseline_seconds: int = 1_800
    min_warmup_seconds: int = 180
    scanner_candidate_count: int = 30
    deep_candidate_count: int = 24
    candidate_refresh_seconds: float = 3.0
    deep_min_residency_seconds: float = 30.0
    deep_switch_margin: float = 4.0
    market_refresh_seconds: float = 300.0
    evaluation_seconds: float = 1.0
    portfolio_publish_seconds: float = 1.0
    runtime_health_seconds: float = 1.0
    no_candidate_warning_seconds: float = 300.0
    orderbook_depth: int = 5
    activity_window_seconds: int = 5
    aggression_window_seconds: int = 5
    momentum_window_seconds: int = 10
    long_momentum_window_seconds: int = 30
    expected_move_window_seconds: int = 30
    ignition_quality: float = 0.82
    pullback_quality: float = 0.76
    fee_cache_seconds: float = 3_600.0
    health_lookback: int = 50


@dataclass(slots=True)
class AppConfig:
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
