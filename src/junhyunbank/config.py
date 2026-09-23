from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SafetyConfig:
    min_order_krw: float = 5_000.0
    market_data_stale_seconds: float = 3.0
    order_guard_seconds: float = 0.8
    max_api_failures: int = 5
    settlement_grace_seconds: float = 8.0
    # Exposure limits are equity/risk fractions, not arbitrary fixed KRW caps.
    # They keep one strong-looking microstructure signal from becoming an
    # effectively all-in order when live liquidity or volatility is unusual.
    cash_reserve_fraction: float = 0.15
    single_position_fraction: float = 0.15
    gross_exposure_fraction: float = 0.60
    per_trade_risk_fraction: float = 0.0025
    portfolio_risk_fraction: float = 0.01
    liquidity_participation_fraction: float = 0.10
    session_drawdown_halt_fraction: float = 0.02
    managed_absence_confirmations: int = 3
    managed_quote_fallback_seconds: float = 2.0
    market_universe_min_size: int = 50
    market_universe_min_retained_fraction: float = 0.70
    market_unknown_alert_fraction: float = 0.05
    event_buffer_max_items: int = 5_000


@dataclass(slots=True)
class StrategyConfig:
    baseline_seconds: int = 1_800
    min_warmup_seconds: int = 180
    min_warmup_trade_seconds: int = 60
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
    min_book_observations: int = 10
    activity_window_seconds: int = 5
    aggression_window_seconds: int = 5
    momentum_window_seconds: int = 10
    long_momentum_window_seconds: int = 30
    expected_move_window_seconds: int = 30
    ignition_quality: float = 0.82
    pullback_quality: float = 0.76
    max_signal_capital_fraction: float = 0.25
    regime_production_universe_size: int = 10
    regime_min_coverage: float = 0.50
    fee_cache_seconds: float = 3_600.0
    max_fee_lookups_per_cycle: int = 1
    health_lookback: int = 50
    exit_confirmation_seconds: float = 5.0
    exit_pressure_deadband: float = 0.10
    max_holding_seconds: float = 900.0


@dataclass(slots=True)
class AppConfig:
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
