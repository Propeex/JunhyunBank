"""Exercise real feature extraction through journalled order execution."""
import math

from junhyunbank.models import EngineState
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


def test_raw_trade_and_book_signal_reaches_buy_and_persists_fill(tmp_path):
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'orders.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
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
    engine._evaluate_cycle()
    assert client.orders==[]
    assert any('거래비용' in e.get('reason','') for e in engine.drain_events())


def test_signal_that_expires_during_candidate_batch_does_not_place_order(tmp_path):
    from junhyunbank.models import Signal, StrategyDecision
    client=Exchange()
    engine=TradingEngine(client,storage=Storage(tmp_path/'orders.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']
    engine._deep_markets=['KRW-X']
    feed(engine.strategy)
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


def test_small_dynamic_allocation_replaces_buy_diagnostic_with_amount_block(tmp_path):
    client=Exchange()
    client.get_accounts=lambda:[{'currency':'KRW','balance':'5000'}]
    engine=TradingEngine(client,storage=Storage(tmp_path/'orders.db'))
    engine._state=EngineState.RUNNING
    engine._allowed_markets=['KRW-X']; engine._deep_markets=['KRW-X']
    feed(engine.strategy)
    engine._evaluate_cycle()
    assert client.orders==[]
    diagnostics=[e for e in engine.drain_events() if e['type']=='entry_diagnostic' and e['market']=='KRW-X']
    final=diagnostics[-1]
    assert '매수 차단' in final['reason']
    assert 0 < final['amount_krw'] < final['min_order_krw']==5000
    assert final['available_cash']==5000
