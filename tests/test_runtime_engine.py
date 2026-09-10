from pathlib import Path

from junhyunbank.config import AppConfig, StrategyConfig
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.storage import Storage


class _DummyClient:
    access_key = "a"
    secret_key = "b"


class _FakeStream:
    instances = []

    def __init__(self, markets, **kwargs):
        self.markets = list(markets)
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.updates = []
        self.connected = True
        self.age_seconds = 0.0
        _FakeStream.instances.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def update_markets(self, markets):
        self.markets = list(markets)
        self.updates.append(list(markets))
        return True


def _engine(tmp_path: Path) -> TradingEngine:
    config = AppConfig(strategy=StrategyConfig(deep_candidate_count=2))
    return TradingEngine(
        _DummyClient(),
        config,
        storage=Storage(tmp_path / "runtime.db"),
    )


def test_deep_candidate_change_reuses_existing_stream(monkeypatch, tmp_path):
    import junhyunbank.runtime_engine as module

    _FakeStream.instances.clear()
    monkeypatch.setattr(module, "MarketStream", _FakeStream)
    engine = _engine(tmp_path)

    engine._restart_deep_stream(["KRW-BTC", "KRW-ETH"])
    first = engine._deep_stream
    assert first is not None
    assert first.started == 1

    engine._restart_deep_stream(["KRW-BTC", "KRW-XRP"])

    assert engine._deep_stream is first
    assert first.stopped == 0
    assert first.updates == [["KRW-BTC", "KRW-XRP"]]
    assert len(_FakeStream.instances) == 1


def test_reentering_deep_market_discards_stale_orderbook_state(monkeypatch, tmp_path):
    import junhyunbank.runtime_engine as module

    _FakeStream.instances.clear()
    monkeypatch.setattr(module, "MarketStream", _FakeStream)
    engine = _engine(tmp_path)
    engine.strategy._books["KRW-XRP"] = object()
    engine.strategy._book_history["KRW-XRP"].append((1, 0.9, 0.1))

    engine._restart_deep_stream(["KRW-XRP"])

    assert "KRW-XRP" not in engine.strategy._books
    assert list(engine.strategy._book_history.get("KRW-XRP", ())) == []


def test_empty_deep_set_stops_stream(monkeypatch, tmp_path):
    import junhyunbank.runtime_engine as module

    _FakeStream.instances.clear()
    monkeypatch.setattr(module, "MarketStream", _FakeStream)
    engine = _engine(tmp_path)
    engine._restart_deep_stream(["KRW-BTC"])
    first = engine._deep_stream

    engine._restart_deep_stream([])

    assert first is not None and first.stopped == 1
    assert engine._deep_stream is None
