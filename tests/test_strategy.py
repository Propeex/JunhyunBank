import time

from junhyunbank.config import StrategyConfig
from junhyunbank.market_stream import TRANSPORT_AGE_SECONDS_KEY
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


def test_duplicate_and_out_of_order_trades_never_mutate_current_frame():
    strategy = MicroFlowStrategy(StrategyConfig())
    first = {
        "code": "KRW-X",
        "trade_price": 100,
        "trade_volume": 2,
        "ask_bid": "BID",
        "trade_timestamp": 1_700_000_001_000,
        "sequential_id": 11,
    }
    assert strategy.on_trade(first)
    original = strategy._series("KRW-X")[-1]

    assert not strategy.on_trade(first)
    assert not strategy.on_trade(
        {
            **first,
            "trade_price": 999,
            "trade_timestamp": 1_700_000_000_000,
            "sequential_id": 10,
        }
    )

    current = strategy._series("KRW-X")[-1]
    assert current.close == original.close == 100
    assert current.trade_value == original.trade_value == 200
    assert current.trades == original.trades == 1


def test_duplicate_and_out_of_order_books_preserve_fresh_snapshot():
    strategy = MicroFlowStrategy(StrategyConfig())
    first = {
        "code": "KRW-X",
        "timestamp": 1_700_000_001_000,
        "orderbook_units": [
            {
                "bid_price": 99,
                "ask_price": 101,
                "bid_size": 2,
                "ask_size": 1,
            }
        ],
    }
    assert strategy.on_orderbook(first)
    original = strategy.book("KRW-X")
    original_history = list(strategy._book_history["KRW-X"])

    assert not strategy.on_orderbook(first)
    assert not strategy.on_orderbook(
        {
            "code": "KRW-X",
            "timestamp": 1_700_000_000_000,
            "orderbook_units": [
                {
                    "bid_price": 199,
                    "ask_price": 201,
                    "bid_size": 100,
                    "ask_size": 1,
                }
            ],
        }
    )

    assert strategy.book("KRW-X") is original
    assert list(strategy._book_history["KRW-X"]) == original_history


def test_stream_transport_age_carries_into_trade_and_book_freshness(monkeypatch):
    now = [500.0]
    monkeypatch.setattr("junhyunbank.strategy.time.monotonic", lambda: now[0])
    strategy = MicroFlowStrategy(StrategyConfig())
    transport_age = 2.8

    assert strategy.on_trade(
        {
            "code": "KRW-X",
            "trade_price": 100,
            "trade_volume": 1,
            "ask_bid": "BID",
            "trade_timestamp": 1_700_000_001_000,
            TRANSPORT_AGE_SECONDS_KEY: transport_age,
        }
    )
    assert strategy.on_orderbook(
        {
            "code": "KRW-X",
            "timestamp": 1_700_000_001_000,
            "orderbook_units": [
                {
                    "bid_price": 99,
                    "ask_price": 101,
                    "bid_size": 2,
                    "ask_size": 1,
                }
            ],
            TRANSPORT_AGE_SECONDS_KEY: transport_age,
        }
    )

    assert abs(strategy.trade_age("KRW-X") - transport_age) < 1e-9
    assert abs(strategy.book_age("KRW-X") - transport_age) < 1e-9
    now[0] += 0.3
    assert strategy.trade_age("KRW-X") > 3.0
    assert strategy.book_age("KRW-X") > 3.0


def test_invalid_stream_transport_age_is_rejected_without_refresh(monkeypatch):
    monkeypatch.setattr("junhyunbank.strategy.time.monotonic", lambda: 500.0)
    strategy = MicroFlowStrategy(StrategyConfig())
    event = {
        "code": "KRW-X",
        "trade_price": 100,
        "trade_volume": 1,
        "trade_timestamp": 1_700_000_001_000,
    }

    assert not strategy.on_trade(
        {**event, TRANSPORT_AGE_SECONDS_KEY: float("nan")}
    )
    assert not strategy.on_trade({**event, TRANSPORT_AGE_SECONDS_KEY: -0.1})
    assert strategy.trade_age("KRW-X") == float("inf")


def test_zero_filled_gaps_do_not_complete_trade_warmup():
    strategy = MicroFlowStrategy(
        StrategyConfig(
            baseline_seconds=100,
            min_warmup_seconds=5,
            min_warmup_trade_seconds=4,
            long_momentum_window_seconds=3,
        )
    )
    base = 1_700_000_000_000
    for second in (0, 5):
        assert strategy.on_trade(
            {
                "code": "KRW-X",
                "trade_price": 100 + second,
                "trade_volume": 1,
                "ask_bid": "BID",
                "timestamp": base + second * 1000,
            }
        )

    assert len(strategy._series("KRW-X")) == 6
    assert strategy.warmup_ratio("KRW-X") == 0.5
    assert strategy._feature_set("KRW-X") is None


def _feed_prices(strategy, prices):
    base = 1_700_000_000_000
    for second, price in enumerate(prices):
        assert strategy.on_trade(
            {
                "code": "KRW-X",
                "trade_price": price,
                "trade_volume": 1,
                "ask_bid": "BID",
                "timestamp": base + second * 1000,
                "sequential_id": second + 1,
            }
        )


def test_pullback_requires_advance_before_peak_before_retracement():
    strategy = MicroFlowStrategy(StrategyConfig())
    # Peak -> crash -> bounce used to pass an unordered-extrema pullback test.
    recent = [120 - (30 * idx / 30) for idx in range(31)]
    recent += [90 + (10 * idx / 28) for idx in range(1, 29)]
    recent += [101]
    _feed_prices(strategy, [105] * 5 + recent)

    assert not strategy._is_pullback("KRW-X", expected_move=0.10)


def test_chronological_advance_retracement_and_recovery_is_pullback():
    strategy = MicroFlowStrategy(StrategyConfig())
    advance = [100] + [100 + (20 * idx / 30) for idx in range(1, 31)]
    retracement = [120 - (6 * idx / 19) for idx in range(1, 20)]
    recovery = [114 + (2 * idx / 10) for idx in range(1, 11)]
    _feed_prices(strategy, [105] * 5 + advance + retracement + recovery)

    assert strategy._is_pullback("KRW-X", expected_move=0.10)


def test_trailing_stop_ratchets_and_is_enforced_without_recalculation():
    strategy = MicroFlowStrategy(StrategyConfig(max_holding_seconds=10_000))
    strategy._feature_set = lambda market: _features()
    strategy.trade_age = lambda market: 0.0
    strategy.book_age = lambda market: 0.0

    first = strategy.evaluate_position(
        "KRW-X",
        entry_price=100,
        current_price=110,
        peak_price=110,
        initial_risk_pct=0.05,
        round_trip_cost_pct=0.001,
        elapsed_seconds=10,
        expected_horizon_seconds=60,
    )
    assert first.signal == Signal.HOLD
    assert first.trailing_stop_price > 100

    second = strategy.evaluate_position(
        "KRW-X",
        entry_price=100,
        current_price=first.trailing_stop_price - 0.01,
        peak_price=105,
        initial_risk_pct=0.05,
        round_trip_cost_pct=0.001,
        elapsed_seconds=11,
        expected_horizon_seconds=60,
        trailing_stop_price=first.trailing_stop_price,
    )
    assert second.signal == Signal.SELL
    assert "Ratchet Trailing Stop" in second.reason
    assert second.trailing_stop_price == first.trailing_stop_price


def test_past_small_mfe_no_longer_disables_expected_horizon_exit():
    strategy = MicroFlowStrategy(StrategyConfig(max_holding_seconds=10_000))
    features = _features()
    features.update(aggression=-0.2, imbalance=-0.2, short_return=-0.001)
    strategy._feature_set = lambda market: features
    strategy.trade_age = lambda market: 0.0
    strategy.book_age = lambda market: 0.0

    decision = strategy.evaluate_position(
        "KRW-X",
        entry_price=100,
        current_price=100.01,
        peak_price=100.20,
        initial_risk_pct=0.05,
        round_trip_cost_pct=0.001,
        elapsed_seconds=91,
        expected_horizon_seconds=60,
    )

    assert decision.signal == Signal.SELL
    assert decision.reason == "기대 시간 내 모멘텀 미발생"


def test_small_universe_without_btc_never_gets_fixture_neutral_regime():
    strategy = MicroFlowStrategy(StrategyConfig(regime_production_universe_size=10))

    regime, factor = strategy.market_regime(
        [f"KRW-{index}" for index in range(9)]
    )

    assert regime == "DATA_INSUFFICIENT"
    assert factor == 0.0
