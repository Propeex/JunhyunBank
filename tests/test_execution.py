import time
from types import SimpleNamespace

import httpx
import pytest

from junhyunbank.models import EngineState, Position, SignalKind
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.storage import Storage
from junhyunbank.upbit import UpbitAPIError, UpbitClient


def fill(side='bid', quantity=2, price=10000, state='cancel'):
    return dict(uuid='exchange-1', market='KRW-X', side=side, state=state,
                executed_volume=str(quantity), paid_fee=str(quantity*price*0.0005),
                trades=[dict(volume=str(quantity),price=str(price),funds=str(quantity*price))] if quantity else [])


class Client:
    access_key = 'test'
    secret_key = 'test'
    def __init__(self):
        self.calls = []
        self.detail = fill()
        self.error = None
    def get_accounts(self):
        return [{'currency':'KRW', 'balance':'100000'}]
    def get_order(self, **kwargs):
        self.calls.append(('lookup',kwargs))
        return self.detail
    def place_best_ioc_buy(self, market, amount, **kwargs):
        self.calls.append(('buy',amount,kwargs))
        if self.error:
            raise self.error
        return {'uuid':'exchange-1'}
    def place_best_ioc_sell(self, market, quantity, **kwargs):
        self.calls.append(('sell',quantity,kwargs))
        return {'uuid':'exchange-1'}
    def place_market_sell(self, market, quantity, **kwargs):
        self.calls.append(('market_sell',quantity,kwargs))
        return {'uuid':'exchange-1'}


@pytest.fixture
def engine(tmp_path):
    e = TradingEngine(Client(), storage=Storage(tmp_path/'test.db'))
    e._state = EngineState.RUNNING
    e._allowed_markets = ['KRW-X']
    e.strategy.trade_age = lambda market: 0
    e.strategy.book_age = lambda market: 0
    return e


def buy(engine):
    engine._buy_managed(market='KRW-X',amount_krw=30000.9,bid_fee=.0005,actual_round_trip_cost=.001,
                        decision=SimpleNamespace(reason='test',initial_risk_pct=.01,score=90,kind=SignalKind.IGNITION,expected_horizon_seconds=60))


def test_partial_ioc_buy_records_actual_funds_and_is_idempotent(engine):
    buy(engine)
    state = engine.storage.get_managed_state('KRW-X')
    assert state['managed_quantity'] == 2
    assert state['entry_amount_krw'] == 20000
    assert state['entry_price'] == 10000
    assert engine.storage.pending_orders() == []
    assert engine.client.calls[0][1] == 30000
    identifier = engine.client.calls[0][2]['identifier']
    assert not engine.storage.complete_order_intent(identifier, fill())
    with engine.storage._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM trades').fetchone()[0] == 1


def test_timeout_is_recovered_by_identifier_after_restart_without_repost(engine):
    engine.client.error = httpx.ReadTimeout('timeout')
    buy(engine)
    pending = engine.storage.pending_orders()
    assert len(pending) == 1
    assert not engine.storage.managed_markets()
    buy(engine)
    assert len([c for c in engine.client.calls if c[0]=='buy']) == 1
    recovered = TradingEngine(engine.client, storage=Storage(engine.storage.path))
    recovered._reconcile_orders()
    assert recovered.storage.get_managed_state('KRW-X')['managed_quantity'] == 2
    assert engine.client.calls[-1] == ('lookup', {'identifier':pending[0]['identifier']})


def test_explicit_rejection_does_not_leave_pending_order(engine):
    engine.client.error = UpbitAPIError('insufficient_funds',400)
    buy(engine)
    assert engine.storage.pending_orders() == []
    assert not engine.storage.managed_markets()


def test_missing_remaining_volume_is_not_terminal(engine):
    responses = iter([dict(uuid='exchange-1',state='wait'), fill()])
    engine.client.get_order = lambda **kwargs: next(responses)
    buy(engine)
    assert engine.storage.get_managed_state('KRW-X')['managed_quantity'] == 2


def test_nonterminal_sell_does_not_trigger_market_fallback(engine):
    engine.storage.mark_managed_position('KRW-X',managed_quantity=2,entry_price=10000)
    engine.config.safety.settlement_grace_seconds = 0
    engine._sell_managed(market='KRW-X',position=Position('KRW-X',10,10000),managed_qty=2,current_price=9000,min_ask_krw=5000,ask_fee=.0005,state={},reason='Emergency Stop')
    assert [c[0] for c in engine.client.calls] == ['sell']
    assert engine.storage.get_managed_state('KRW-X')['managed_quantity'] == 2
    assert len(engine.storage.pending_orders()) == 1


def test_partial_sells_preserve_unrelated_balance_and_aggregate_outcome(engine):
    buy(engine)
    for qty in (1,1):
        engine.client.detail = fill(side='ask',quantity=qty,price=11000)
        state = engine.storage.get_managed_state('KRW-X')
        engine._sell_managed(market='KRW-X',position=Position('KRW-X',20,10000),managed_qty=state['managed_quantity'],current_price=11000,min_ask_krw=5000,ask_fee=.0005,state=state,reason='exit')
    assert [c[1] for c in engine.client.calls if c[0]=='sell'] == [2,1]
    assert not engine.storage.managed_markets()
    outcomes = engine.storage.strategy_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0] == pytest.approx(.09895/.01)


@pytest.mark.parametrize('state',['wait','watch',None])
def test_nonterminal_journal_never_applies_partial_fill(engine,state):
    engine.storage.create_order_intent('x','KRW-X','BUY',{})
    assert not engine.storage.complete_order_intent('x',fill(state=state))
    assert engine.storage.pending_orders()


def test_terminal_fill_missing_trade_details_remains_pending(engine):
    engine.storage.create_order_intent('x','KRW-X','BUY',{})
    detail=fill(); detail['trades']=[]
    assert not engine.storage.complete_order_intent('x',detail)
    assert not engine.storage.managed_markets()


@pytest.mark.parametrize('stop',['drain','emergency','stale','removed'])
def test_buy_rechecks_last_moment_guards(engine,stop):
    if stop=='drain': engine._state=EngineState.DRAINING
    if stop=='emergency': engine.emergency_stop()
    if stop=='stale': engine.strategy.book_age=lambda m: 10
    if stop=='removed': engine._allowed_markets=[]
    buy(engine)
    assert not engine.client.calls
    assert not engine.storage.pending_orders()


def test_stop_during_intent_write_prevents_submission(engine,monkeypatch):
    original=engine.storage.create_order_intent
    def stop(*args):
        original(*args)
        engine.emergency_stop()
    monkeypatch.setattr(engine.storage,'create_order_intent',stop)
    buy(engine)
    assert not engine.client.calls
    assert not engine.storage.pending_orders()


def test_order_post_429_is_not_automatically_retried():
    client=UpbitClient('access','x'*64)
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(429,json={'error':{'name':'too_many_requests','message':'limited'}})
    client.http.close()
    client.http=httpx.Client(base_url=client.BASE_URL,transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(UpbitAPIError): client.place_best_ioc_buy('KRW-X',5000,identifier='test')
    finally:
        client.close()
    assert len(calls)==1


def test_balance_network_error_does_not_escape_evaluation(engine):
    def fail(): raise httpx.ReadTimeout('temporary')
    engine.client.get_accounts=fail
    engine._evaluate_cycle()
    assert engine.state == EngineState.RUNNING
    assert any('잔고 조회 실패' in e.get('reason','') for e in engine.drain_events())


def test_zero_managed_quantity_is_never_replaced_with_account_balance(engine):
    engine.storage.mark_managed_position('KRW-X',managed_quantity=0)
    engine._live_portfolio=lambda: (100000,100000,100000,[Position('KRW-X',99,10000)])
    engine._evaluate_cycle()
    assert engine.storage.get_managed_state('KRW-X')['managed_quantity']==0
    assert not engine.client.calls
