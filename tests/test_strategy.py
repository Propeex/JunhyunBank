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
    return BookSnapshot(
        time.monotonic(),
        [99.9] * 5,
        [100.0] * 5,
        [100.0] * 5,
        [100.0] * 5,
        0.3,
        0.0002,
        0.001,
    )


def test_trade_ingestion_tracks_latest_price():
    strategy = MicroFlowStrategy(
        StrategyConfig(min_warmup_seconds=3, long_momentum_window_seconds=3)
    )
    base = 1_700_000_000_000
    for i in range(4):
        strategy.on_trade(
            {
                "code": "KRW-TEST",
                "trade_price": 100 + i,
                "trade_volume": 1,
                "ask_bid": "BID",
                "timestamp": base + i * 1000,
            }
        )
    assert strategy.latest_price("KRW-TEST") == 103
    assert strategy.warmup_ratio("KRW-TEST") == 1.0


def test_missing_trade_seconds_are_zero_filled_on_clock_time_axis():
    strategy = MicroFlowStrategy(
        StrategyConfig(
            baseline_seconds=100,
            min_warmup_seconds=3,
            long_momentum_window_seconds=3,
        )
    )
    base = 1_700_000_000_000
    strategy.on_trade(
        {
            "code": "KRW-TEST",
            "trade_price": 100,
            "trade_volume": 1,
            "ask_bid": "BID",
            "timestamp": base,
        }
    )
    strategy.on_trade(
        {
            "code": "KRW-TEST",
            "trade_price": 105,
            "trade_volume": 1,
            "ask_bid": "BID",
            "timestamp": base + 5000,
        }
    )
    frames = strategy._series("KRW-TEST")
    assert len(frames) == 6
    assert [frame.second for frame in frames] == list(
        range(frames[0].second, frames[0].second + 6)
    )
    assert [frame.trade_value for frame in frames[1:5]] == [0.0] * 4


def test_cost_gate_rejects_signal_without_net_room():
    strategy = MicroFlowStrategy(StrategyConfig(ignition_quality=0.7))
    strategy._feature_set = lambda market: _features(expected_move=0.001)
    strategy._books["KRW-TEST"] = _book()
    strategy._is_pullback = lambda market, expected: False
    decision = strategy.evaluate_entry(
        "KRW-TEST",
        bid_fee=0.0005,
        ask_fee=0.0005,
        health=1.0,
        regime_factor=1.0,
    )
    assert decision.signal == Signal.HOLD


def test_high_quality_relative_signal_can_buy():
    strategy = MicroFlowStrategy(StrategyConfig(ignition_quality=0.7))
    strategy._feature_set = lambda market: _features(expected_move=0.02)
    strategy._books["KRW-TEST"] = _book()
    strategy._is_pullback = lambda market, expected: False
    decision = strategy.evaluate_entry(
        "KRW-TEST",
        bid_fee=0.0005,
        ask_fee=0.0005,
        health=1.0,
        regime_factor=1.0,
    )
    assert decision.signal == Signal.BUY
    assert 0 < decision.capital_fraction <= 1


def test_emergency_stop_distance_does_not_expand_after_entry():
    strategy = MicroFlowStrategy(StrategyConfig())
    strategy._feature_set = lambda market: _features()
    decision = strategy.evaluate_position(
        "KRW-TEST",
        entry_price=100,
        current_price=94,
        peak_price=100,
        initial_risk_pct=0.05,
        round_trip_cost_pct=0.001,
        elapsed_seconds=5,
        expected_horizon_seconds=60,
    )
    assert decision.signal == Signal.SELL
    assert "Emergency Stop" in decision.reason


def test_trade_series_is_a_snapshot_not_a_mutable_current_frame():
    strategy=MicroFlowStrategy(StrategyConfig())
    event=dict(code='KRW-X',trade_price=100,trade_volume=1,timestamp=1700000000000)
    strategy.on_trade(event)
    frames=strategy._series('KRW-X')
    strategy.on_trade({**event,'trade_price':110})
    assert frames[-1].close==100


def test_reentry_reset_is_observed_even_when_cost_gate_fails():
    strategy=MicroFlowStrategy(StrategyConfig())
    strategy._feature_set=lambda m:_features(expected_move=.00001,quality=.2)
    strategy._books['KRW-X']=_book()
    strategy.notify_exit('KRW-X')
    strategy.evaluate_entry('KRW-X',bid_fee=.0005,ask_fee=.0005,health=1,regime_factor=1)
    assert 'KRW-X' not in strategy._blocked_after_exit


def test_aggression_compares_equal_duration_windows(monkeypatch):
    import junhyunbank.strategy as module
    strategy=MicroFlowStrategy(StrategyConfig(min_warmup_seconds=40))
    for i in range(60):
        strategy.on_trade(dict(code='KRW-X',trade_price=100+i,trade_volume=1,ask_bid='BID' if i%2 else 'ASK',timestamp=1700000000000+i*1000))
    captured=[]
    original=module._percentile_rank
    def capture(history,value):
        captured.append(history)
        return original(history,value)
    monkeypatch.setattr(module,'_percentile_rank',capture)
    strategy._feature_set('KRW-X')
    aggression_history=captured[1]
    assert len(aggression_history)==55
    assert all(-.3 < value < .3 for value in aggression_history)
