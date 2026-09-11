import math

import pytest

from junhyunbank.config import StrategyConfig
from junhyunbank.models import Signal
from junhyunbank.strategy import MicroFlowStrategy


def trade_and_book(strategy, second, price=100, strength=2):
    stamp=1700000000000+second*1000
    strategy.on_trade(dict(code='KRW-X',timestamp=stamp,trade_price=price,trade_volume=10,ask_bid='BID'))
    strategy.on_orderbook(dict(code='KRW-X',timestamp=stamp,orderbook_units=[dict(bid_price=price-.005,ask_price=price+.005,bid_size=100*strength,ask_size=100)]))


def position(strategy, elapsed, **changes):
    kwargs=dict(entry_price=100,current_price=100,peak_price=100,initial_risk_pct=.02,
                round_trip_cost_pct=.001,elapsed_seconds=elapsed,expected_horizon_seconds=60)
    kwargs.update(changes)
    return strategy.evaluate_position('KRW-X',**kwargs)


@pytest.fixture
def strategy():
    s=MicroFlowStrategy(StrategyConfig())
    s.features=dict(activity_q=0,aggression_q=0,book_q=0,momentum_q=0,
                    aggression=.4,imbalance=.3,short_return=0,
                    realized_30s=.002,spread_pct=.0001)
    s._feature_set=lambda m: s.features
    trade_and_book(s,0)
    return s


def test_low_entry_percentiles_do_not_imply_exit(strategy):
    for second in range(1,12):
        trade_and_book(strategy,second)
        assert position(strategy,second).signal==Signal.HOLD


def test_raw_positive_book_plateau_reproduces_old_false_exit_without_new_sell():
    s=MicroFlowStrategy(StrategyConfig())
    for second in range(240):
        trade_and_book(s,second,100*(1+.005*math.sin(second/10)),strength=2+.5*math.sin(second/7))
    entry=s.latest_price('KRW-X')
    old_weak=0
    for second in range(240,255):
        trade_and_book(s,second,entry,strength=3)
        f=s._feature_set('KRW-X')
        old_hold=math.prod(max(1e-6,f[k]) for k in ['activity_q','aggression_q','book_q','momentum_q'])**.25
        old_weak += old_hold < .36
        assert position(s,second-239,entry_price=entry,current_price=entry,peak_price=entry).signal==Signal.HOLD
    assert old_weak>=2  # The previous two-poll rule would have sold.


def test_sustained_two_axis_reversal_exits_after_time_and_new_observations(strategy):
    strategy.features.update(aggression=-.7,imbalance=-.7,short_return=-.001)
    for second in range(6):
        trade_and_book(strategy,second)
        decision=position(strategy,second)
        assert decision.signal == (Signal.SELL if second==5 else Signal.HOLD)
    assert '지속 수급 반전' in decision.reason


def test_repeated_poll_of_same_frame_never_counts_as_confirmation(strategy):
    strategy.features.update(aggression=-.7,imbalance=-.7,short_return=-.001)
    for elapsed in range(12):
        assert position(strategy,elapsed).signal==Signal.HOLD


def test_only_one_negative_axis_does_not_trigger_flow_exit(strategy):
    strategy.features.update(aggression=-.7,imbalance=.3,short_return=.001)
    for second in range(12):
        trade_and_book(strategy,second)
        assert position(strategy,second).signal==Signal.HOLD


def test_recovered_support_resets_confirmation(strategy):
    strategy.features.update(aggression=-.7,imbalance=-.7,short_return=-.001)
    for second in range(4):
        trade_and_book(strategy,second)
        position(strategy,second)
    strategy.features.update(aggression=.4,imbalance=.3,short_return=.001)
    trade_and_book(strategy,4)
    position(strategy,4)
    strategy.features.update(aggression=-.7,imbalance=-.7,short_return=-.001)
    for second in range(5,10):
        trade_and_book(strategy,second)
        assert position(strategy,second).signal==Signal.HOLD


def test_stale_book_resets_flow_evidence(strategy):
    strategy.features.update(aggression=-.7,imbalance=-.7,short_return=-.001)
    for second in range(4):
        trade_and_book(strategy,second)
        position(strategy,second)
    strategy.book_age=lambda m:10
    assert position(strategy,4).signal==Signal.HOLD
    assert not strategy._weak_evidence


def test_unobserved_gap_does_not_count_towards_confirmation(strategy):
    strategy.features.update(aggression=-.7,imbalance=-.7,short_return=-.001)
    for second in [0,1,2,10]:
        trade_and_book(strategy,second)
        assert position(strategy,second).signal==Signal.HOLD


def test_emergency_stop_is_immediate_even_on_stale_data(strategy):
    strategy.book_age=lambda m:100
    strategy.trade_age=lambda m:100
    decision=position(strategy,.1,current_price=97)
    assert decision.signal==Signal.SELL
    assert decision.reason.startswith('Emergency Stop')


def test_profit_trailing_does_not_wait_for_book_or_flow_confirmation(strategy):
    strategy.book_age=lambda m:100
    decision=position(strategy,1,current_price=101,peak_price=102)
    assert decision.signal==Signal.SELL
    assert 'Trailing' in decision.reason


def test_no_progress_timeout_still_exits_even_with_positive_book(strategy):
    assert position(strategy,30).signal==Signal.HOLD
    assert position(strategy,91).signal==Signal.SELL


def test_reset_book_clears_exit_evidence(strategy):
    strategy.features.update(aggression=-.7,imbalance=-.7)
    position(strategy,0)
    strategy.reset_orderbook('KRW-X')
    assert not strategy._weak_evidence
