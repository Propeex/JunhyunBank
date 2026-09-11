from junhyunbank.config import StrategyConfig
from junhyunbank.strategy import MicroFlowStrategy
from junhyunbank.models import Signal
from test_entry_pipeline import feed


def plateau(strategy, seconds=11, falling=False, gap=False):
    price = strategy.latest_price('KRW-X')
    for i in range(seconds):
        strength = 7 - i * .1 if falling else 7
        strategy.on_orderbook({'code':'KRW-X','timestamp':1700000240000+i*(3000 if gap else 1000),
            'orderbook_units':[{'bid_price':price-.005,'ask_price':price+.005,
                               'bid_size':10000*strength,'ask_size':10000}]})


def test_sustained_strong_book_can_enter_without_endless_acceleration():
    strategy = MicroFlowStrategy(StrategyConfig()); feed(strategy); plateau(strategy)
    decision = strategy.evaluate_entry('KRW-X', bid_fee=.0005, ask_fee=.0005, health=1, regime_factor=1)
    assert strategy._feature_set('KRW-X')['book_q'] > .8
    assert decision.signal == Signal.BUY


def test_short_plateau_does_not_establish_sustained_pressure():
    strategy = MicroFlowStrategy(StrategyConfig()); feed(strategy)
    strategy._book_history['KRW-X'].clear()
    plateau(strategy, seconds=4)
    assert strategy._feature_set('KRW-X')['book_q'] < .1


def test_falling_book_does_not_qualify_as_sustained():
    strategy = MicroFlowStrategy(StrategyConfig()); feed(strategy); plateau(strategy, falling=True)
    assert strategy._feature_set('KRW-X')['book_q'] < .1


def test_gaps_do_not_qualify_as_sustained():
    strategy = MicroFlowStrategy(StrategyConfig()); feed(strategy); plateau(strategy, gap=True)
    assert strategy._feature_set('KRW-X')['book_q'] < .1


def test_sustained_pressure_does_not_bypass_cost_gate():
    strategy = MicroFlowStrategy(StrategyConfig()); feed(strategy); plateau(strategy)
    decision = strategy.evaluate_entry('KRW-X', bid_fee=.1, ask_fee=.1, health=1, regime_factor=1)
    assert decision.signal == Signal.HOLD
    assert '거래비용' in decision.reason
