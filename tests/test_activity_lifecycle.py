import time

from junhyunbank.engine import TradingEngine
from junhyunbank.models import EngineState
from junhyunbank.storage import Storage


def offline_engine(tmp_path, monkeypatch):
    engine = TradingEngine(object(), storage=Storage(tmp_path / 'activity.db'))
    engine._allowed_markets = ['KRW-X']
    engine._run_started_at = time.monotonic()
    monkeypatch.setattr(engine, '_refresh_markets', lambda: None)
    monkeypatch.setattr(engine, '_restart_deep_stream', lambda markets: None)
    monkeypatch.setattr(engine, '_publish_portfolio', lambda: None)
    return engine


def latest_activity_before(events, event_type):
    index = next(i for i, event in enumerate(events) if event['type'] == event_type)
    return next(event for event in reversed(events[:index]) if event['type'] == 'activity')


def test_last_drained_sell_is_visible_before_drain_and_stop(tmp_path, monkeypatch):
    engine = offline_engine(tmp_path, monkeypatch)
    engine._state = EngineState.DRAINING
    engine.storage.mark_managed_position(
        'KRW-X', entry_price=10000, entry_amount_krw=20000,
        entry_fee_rate=.0005, initial_risk_pct=.01, managed_quantity=2,
    )
    engine.storage.create_order_intent('last-sell', 'KRW-X', 'SELL', {'reason': 'final exit'})

    def settle_final_sell():
        assert engine.storage.complete_order_intent('last-sell', {
            'uuid': 'confirmed-sell', 'state': 'done', 'executed_volume': '2',
            'paid_fee': '10', 'trades': [{'volume': '2', 'price': '10000', 'funds': '20000'}],
        })

    monkeypatch.setattr(engine, '_evaluate_cycle', settle_final_sell)
    engine._run()
    events = engine.drain_events()
    first = next(event for event in events if event['type'] == 'activity')
    assert first['trades'] == [] and len(first['pending']) == 1
    for terminal in ('drain_complete', 'stopped'):
        final = latest_activity_before(events, terminal)
        assert final['counts'] == {'SELL': 1}
        assert final['pending'] == []
        assert final['evaluation_cycles'] == 1
        assert len(final['trades']) == 1 and final['trades'][0]['side'] == 'SELL'


def test_immediate_stop_keeps_unresolved_order_separate_from_fills(tmp_path, monkeypatch):
    engine = offline_engine(tmp_path, monkeypatch)
    engine.storage.create_order_intent('unresolved', 'KRW-X', 'BUY', {})
    engine._hard_stop.set()
    engine._run()
    final = latest_activity_before(engine.drain_events(), 'stopped')
    assert final['counts'] == {} and final['trades'] == []
    assert len(final['pending']) == 1


def test_dashboard_read_failure_does_not_prevent_shutdown(tmp_path, monkeypatch):
    engine = offline_engine(tmp_path, monkeypatch)

    def unavailable():
        raise RuntimeError('fixture read failure')

    monkeypatch.setattr(engine.storage, 'activity_snapshot', unavailable)
    engine._hard_stop.set()
    engine._run()
    events = engine.drain_events()
    assert engine.state == EngineState.STOPPED
    assert any(event['type'] == 'warning' and 'fixture read failure' in event['message'] for event in events)
    assert events[-1]['type'] == 'stopped'
    assert not any(event['type'] == 'activity' for event in events)
