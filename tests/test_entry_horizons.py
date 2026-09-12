import pytest
from junhyunbank.config import StrategyConfig
from junhyunbank.models import Signal
from junhyunbank.strategy import MicroFlowStrategy, SecondFrame
from test_strategy import _features, _book


def strategy(rate, count=360):
    s=MicroFlowStrategy(StrategyConfig())
    frames=[]
    for i in range(count):
        p=100*(1+rate)**i
        frames.append(SecondFrame(i,p,p,p,p))
    s._series=lambda market:frames
    # Isolate horizon choice from independent entry quality gates.
    s._feature_set=lambda market:_features(expected_move=(1+rate)**30-1)
    s._books['KRW-X']=_book()
    s._is_pullback=lambda *args:False
    return s


@pytest.mark.parametrize('rate,window',[(.00006,60),(.00003,120),(.0002,30)])
def test_shortest_cost_covering_horizon_and_matching_hold_time(rate,window):
    s=strategy(rate)
    decision=s.evaluate_entry('KRW-X',bid_fee=.0005,ask_fee=.0005,health=1,regime_factor=1)
    assert decision.signal==Signal.BUY
    assert f'{window}초 관측' in decision.reason
    assert decision.expected_horizon_seconds>=window
    assert decision.expected_move_pct>decision.round_trip_cost_pct*2


def test_longer_horizon_requires_enough_history():
    s=strategy(.00003,count=180)
    decision=s.evaluate_entry('KRW-X',bid_fee=.0005,ask_fee=.0005,health=1,regime_factor=1)
    assert decision.signal==Signal.HOLD


def test_no_horizon_covers_cost():
    s=strategy(.000001)
    assert s.evaluate_entry('KRW-X',bid_fee=.0005,ask_fee=.0005,health=1,regime_factor=1).signal==Signal.HOLD


def test_long_horizon_does_not_bypass_quality_or_immediate_stop():
    s=strategy(.00003)
    s._feature_set=lambda market:_features(expected_move=.0009,quality=.2)
    assert s.evaluate_entry('KRW-X',bid_fee=.0005,ask_fee=.0005,health=1,regime_factor=1).signal==Signal.HOLD
    d=s.evaluate_position('KRW-X',entry_price=100,current_price=94,peak_price=100,
                          initial_risk_pct=.05,round_trip_cost_pct=.001,elapsed_seconds=1,expected_horizon_seconds=120)
    assert d.signal==Signal.SELL and 'Emergency Stop' in d.reason
