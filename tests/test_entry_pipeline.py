"""Exercise real feature extraction through journalled order execution."""
import math
import time
from dataclasses import replace

from junhyunbank.models import EngineState, Signal, StrategyDecision
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.storage import Storage


class Exchange:
    access_key='test'
    secret_key='test'
    def __init__(self): self.orders=[]
    def get_accounts(self): return [{'currency':'KRW','balance':'100000'}]
    def get_order_chance(self,market): return {'bid_fee':'.0005','ask_fee':'.0005','market':{'bid':{'min_total':'5000'},'ask':{'min_total':'5000'}}}
    def place_best_ioc_buy(self,market,amount,*,identifier):
        self.orders.append((market,amount,identifier))
        return {'uuid':'fixture-order'}
    def get_order(self,**kwargs):
        return {'uuid':'fixture-order','market':'KRW-X','side':'bid','state':'cancel','executed_volume':'100',
                'paid_fee':'5','trades':[{'price':'100','volume':'100','funds':'10000'}]}


def feed(strategy):
    for i in range(240):
        price=100*(1+.01*math.sin(i/10))
        if i>=230:
            price=100*(1+.01*math.sin(23))*(1+.003*(i-230))
        strategy.on_trade({'code':'KRW-X','timestamp':1700000000000+i*1000,'trade_price':price,
                           'trade_volume':100 if i>=235 else 1,'ask_bid':'BID' if i>=235 or i%2 else 'ASK'})
        strength=1+.2*math.sin(i/7) if i<235 else 2+(i-235)
        strategy.on_orderbook({'code':'KRW-X','timestamp':1700000000000+i*1000,'orderbook_units':[
            {'bid_price':price-.005,'ask_price':price+.005,'bid_size':10000*strength,'ask_size':10000}]})


def allow_fixture_regime(engine):
    """Keep these tests focused on the entry pipeline, not universe coverage."""
    engine.strategy.market_regime = lambda _: ("TEST", 1.0)


def test_raw_trade_and_book_signal_reaches_buy_and_persists_fill(tmp_path):
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'orders.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    allow_fixture_regime(engine)
    engine._evaluate_cycle()
    assert len(client.orders)==1
    assert engine.storage.get_managed_state('KRW-X')['managed_quantity']==100
    assert not engine.storage.pending_orders()


def test_same_raw_signal_with_unaffordable_cost_submits_nothing(tmp_path):
    client=Exchange()
    client.get_order_chance=lambda market:{'bid_fee':'.1','ask_fee':'.1'}
    engine=TradingEngine(client,storage=Storage(tmp_path/'orders.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    allow_fixture_regime(engine)
    engine._evaluate_cycle()
    assert client.orders==[]
    assert any('거래비용' in e.get('reason','') for e in engine.drain_events())


def test_signal_that_expires_during_candidate_batch_does_not_place_order(tmp_path):
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'orders.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    allow_fixture_regime(engine)
    original=engine.strategy.evaluate_entry
    calls=[]
    def evaluate(*args,**kwargs):
        calls.append(1)
        if len(calls)==1: return original(*args,**kwargs)
        return StrategyDecision(Signal.HOLD,20,'상승 모멘텀 미확인')
    engine.strategy.evaluate_entry=evaluate
    engine._evaluate_cycle()
    assert len(calls)==2
    assert client.orders==[]
    assert any('주문 직전 재확인' in e.get('reason','') for e in engine.drain_events())


def test_signal_that_expires_after_intent_is_created_never_reaches_post(tmp_path):
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'final-signal.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    allow_fixture_regime(engine)
    original=engine.strategy.evaluate_entry
    calls=[]

    def evaluate(*args,**kwargs):
        calls.append(1)
        if len(calls) < 3:
            return original(*args,**kwargs)
        return StrategyDecision(Signal.HOLD,20,'제출 경계에서 신호 소멸')

    engine.strategy.evaluate_entry=evaluate
    engine._evaluate_cycle()

    assert len(calls)==3
    assert client.orders==[]
    assert engine.storage.pending_orders()==[]
    assert any('POST 직전 매수 재확인 해제' in e.get('reason','')
               for e in engine.drain_events())


def test_orderbook_generation_change_inside_final_buy_guard_blocks_post(
    tmp_path, monkeypatch
):
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'final-generation.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    allow_fixture_regime(engine)
    original_book=engine.strategy.book('KRW-X')
    changed_book=replace(
        original_book,
        received_at=time.monotonic(),
        ask_sizes=[size + 1.0 for size in original_book.ask_sizes],
    )
    books=iter([original_book, original_book, changed_book])
    monkeypatch.setattr(engine.strategy, 'book', lambda _: next(books))

    engine._evaluate_cycle()

    assert client.orders==[]
    assert engine.storage.pending_orders()==[]
    assert any('호가 세대가 변경' in e.get('reason','')
               for e in engine.drain_events())


def test_best_ioc_capacity_shrink_inside_final_buy_guard_blocks_post(
    tmp_path, monkeypatch
):
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'final-capacity.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    allow_fixture_regime(engine)
    original_book=engine.strategy.book('KRW-X')
    thin_book=replace(
        original_book,
        received_at=time.monotonic(),
        bid_sizes=[0.01 for _ in original_book.bid_sizes],
        ask_sizes=[0.01 for _ in original_book.ask_sizes],
    )
    books=iter([original_book, thin_book])
    monkeypatch.setattr(engine.strategy, 'book', lambda _: next(books))

    engine._evaluate_cycle()

    assert client.orders==[]
    assert engine.storage.pending_orders()==[]
    assert any('최우선 호가 유동성이 줄어' in e.get('reason','')
               for e in engine.drain_events())
