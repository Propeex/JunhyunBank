import threading
import pytest
from junhyunbank.config import StrategyConfig
from junhyunbank.strategy import MicroFlowStrategy
from test_entry_pipeline import feed


@pytest.mark.parametrize('window',[3,5,10])
def test_scanner_score_matches_full_feature_score(window):
    strategy=MicroFlowStrategy(StrategyConfig(activity_window_seconds=window)); feed(strategy)
    f=strategy._feature_set('KRW-X')
    expected=(max(1e-6,f['activity_q'])*max(1e-6,f['aggression_q'])*max(1e-6,f['momentum_q']))**(1/3)*100
    assert strategy.hot_score('KRW-X') == expected


def test_ranking_math_does_not_block_trade_ingestion_or_change_snapshot():
    strategy=MicroFlowStrategy(StrategyConfig()); feed(strategy)
    original=strategy._trade_features
    ingested=threading.Event()
    workers=[]
    def compute(frames):
        price=frames[-1].close
        def ingest():
            strategy.on_trade({'code':'KRW-X','timestamp':1700000239000,
                               'trade_price':price+1,'trade_volume':1,'ask_bid':'BID'})
            ingested.set()
        worker=threading.Thread(target=ingest); workers.append(worker); worker.start()
        assert ingested.wait(2), 'Ranking held the ingestion lock during arithmetic'
        assert frames[-1].close==price
        return original(frames)
    strategy._trade_features=compute
    strategy.hot_score('KRW-X')
    for worker in workers: worker.join(2)


def test_scanner_keeps_stale_trade_exclusion(monkeypatch):
    strategy=MicroFlowStrategy(StrategyConfig()); feed(strategy)
    monkeypatch.setattr(strategy,'trade_age',lambda m:11)
    assert strategy.rank_markets(['KRW-X'],30)==[]


def test_runtime_samples_are_throttled_and_missing_ages_are_serializable(tmp_path, monkeypatch):
    import json
    from junhyunbank.engine import TradingEngine
    from junhyunbank.storage import Storage
    from junhyunbank import __version__
    engine=TradingEngine(object(),storage=Storage(tmp_path/'runtime.db'))
    monkeypatch.setattr('junhyunbank.engine.time.monotonic',lambda:100)
    engine._publish_runtime_health([])
    samples=[e for e in engine.drain_events() if e['type']=='runtime_sample']
    assert len(samples)==1 and samples[0]['version']==__version__
    assert samples[0]['trade_age'] is None and samples[0]['book_age'] is None
    json.dumps(samples,allow_nan=False)
    engine._publish_runtime_health([])
    assert not any(e['type']=='runtime_sample' for e in engine.drain_events())
    monkeypatch.setattr('junhyunbank.engine.time.monotonic',lambda:161)
    engine._publish_runtime_health([])
    assert sum(e['type']=='runtime_sample' for e in engine.drain_events())==1
