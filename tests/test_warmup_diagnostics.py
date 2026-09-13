from junhyunbank.engine import TradingEngine
from junhyunbank.storage import Storage


def test_warm_history_and_recent_activity_are_separate(tmp_path,monkeypatch):
    engine=TradingEngine(object(),storage=Storage(tmp_path/'state.db'))
    engine._allowed_markets=['KRW-FRESH','KRW-QUIET','KRW-NEW']
    monkeypatch.setattr(engine.strategy,'latest_price',lambda market:100)
    monkeypatch.setattr(engine.strategy,'warmup_ratio',lambda market:.5 if market=='KRW-NEW' else 1)
    monkeypatch.setattr(engine.strategy,'trade_age',lambda market:45 if market=='KRW-QUIET' else .1)
    engine._publish_runtime_health([])
    health=next(e for e in engine.drain_events() if e['type']=='runtime_health')
    assert health['history_ready_markets']==2
    assert health['warmed_markets']==1
