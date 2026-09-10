import time

from junhyunbank.config import StrategyConfig
from junhyunbank.models import Signal
from junhyunbank.strategy import BookSnapshot, MicroFlowStrategy


def _features(expected_move=0.02, quality=0.95):
    return {
        "activity_q": 0.98,
        "aggression_q": 0.95,
        "book_q": 0.94,
        "momentum_q": 0.96,
        "quality": quality,
        "aggression": 0.4,
        "imbalance": 0.3,
        "micro_bias": 0.0002,
        "short_return": 0.01,
        "long_return": 0.02,
        "expected_move": expected_move,
        "realized_30s": 0.004,
        "spread_pct": 0.0002,
    }


def _book():
    return BookSnapshot(time.monotonic(), [99.9] * 5, [100.0] * 5, [100.0] * 5, [100.0] * 5, 0.3, 0.0002, 0.001)


def test_trade_ingestion_tracks_latest_price():
    strategy = MicroFlowStrategy(StrategyConfig(min_warmup_seconds=3, long_momentum_window_seconds=3))
    base = 1_700_000_000_000
    for i in range(4):
        strategy.on_trade({"code": "KRW-TEST", "trade_price": 100 + i, "trade_volume": 1, "ask_bid": "BID", "timestamp": base + i * 1000})
    assert strategy.latest_price("KRW-TEST") == 103
    assert strategy.warmup_ratio("KRW-TEST") == 1.0


def test_cost_gate_rejects_signal_without_net_room():
    strategy = MicroFlowStrategy(StrategyConfig(ignition_quality=0.7))
    strategy._feature_set = lambda market: _features(expected_move=0.001)
    strategy._books["KRW-TEST"] = _book()
    strategy._is_pullback = lambda market, expected: False
    decision = strategy.evaluate_entry("KRW-TEST", bid_fee=0.0005, ask_fee=0.0005, health=1.0, regime_factor=1.0)
    assert decision.signal == Signal.HOLD


def test_high_quality_relative_signal_can_buy():
    strategy = MicroFlowStrategy(StrategyConfig(ignition_quality=0.7))
    strategy._feature_set = lambda market: _features(expected_move=0.02)
    strategy._books["KRW-TEST"] = _book()
    strategy._is_pullback = lambda market, expected: False
    decision = strategy.evaluate_entry("KRW-TEST", bid_fee=0.0005, ask_fee=0.0005, health=1.0, regime_factor=1.0)
    assert decision.signal == Signal.BUY
    assert 0 < decision.capital_fraction <= 1


def test_emergency_stop_distance_does_not_expand_after_entry():
    strategy = MicroFlowStrategy(StrategyConfig())
    strategy._feature_set = lambda market: _features()
    decision = strategy.evaluate_position("KRW-TEST", entry_price=100, current_price=94, peak_price=100, initial_risk_pct=0.05, round_trip_cost_pct=0.001, elapsed_seconds=5, expected_horizon_seconds=60)
    assert decision.signal == Signal.SELL
    assert "Emergency Stop" in decision.reason
