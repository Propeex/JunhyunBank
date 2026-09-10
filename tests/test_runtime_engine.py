from pathlib import Path

import pytest

from junhyunbank.config import AppConfig, StrategyConfig
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.storage import Storage


class _DummyClient:
    access_key = "a"
    secret_key = "b"


class _MarketClient(_DummyClient):
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get_markets(self):
        self.calls += 1
        return self.rows


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


def _engine(tmp_path: Path, client=None) -> TradingEngine:
    config = AppConfig(strategy=StrategyConfig(deep_candidate_count=2))
    return TradingEngine(
        client or _DummyClient(),
        config,
        storage=Storage(tmp_path / "runtime.db"),
    )


def _normal_event(**overrides):
    event = {
        "warning": False,
        "caution": {
            "PRICE_FLUCTUATIONS": False,
            "TRADING_VOLUME_SOARING": False,
            "DEPOSIT_AMOUNT_SOARING": False,
            "GLOBAL_PRICE_DIFFERENCES": False,
            "CONCENTRATION_OF_SMALL_ACCOUNTS": False,
        },
    }
    event.update(overrides)
    return event


def test_nonempty_all_false_caution_dict_is_not_warning():
    row = {"market": "KRW-BTC", "market_event": _normal_event()}
    flagged, understood = TradingEngine._market_alert_status(row)
    assert understood is True
    assert flagged is False
    assert TradingEngine._is_warning_market(row) is False


def test_alert_parser_does_not_treat_string_false_as_true():
    row = {
        "market": "KRW-BTC",
        "market_event": {
            "warning": "false",
            "caution": {
                "PRICE_FLUCTUATIONS": "false",
                "TRADING_VOLUME_SOARING": "0",
            },
        },
    }
    flagged, understood = TradingEngine._market_alert_status(row)
    assert understood is True
    assert flagged is False
    assert TradingEngine._is_warning_market(row) is False


def test_alert_parser_still_excludes_explicit_caution():
    row = {
        "market": "KRW-X",
        "market_event": _normal_event(
            caution={"TRADING_VOLUME_SOARING": True}
        ),
    }
    flagged, understood = TradingEngine._market_alert_status(row)
    assert understood is True
    assert flagged is True
    assert TradingEngine._is_warning_market(row) is True


def test_unknown_alert_shape_fails_closed():
    row = {
        "market": "KRW-X",
        "market_event": {
            "warning": {"state": "false"},
            "caution": {},
        },
    }
    flagged, understood = TradingEngine._market_alert_status(row)
    assert flagged is False
    assert understood is False
    assert TradingEngine._is_warning_market(row) is True


def test_refresh_markets_builds_nonzero_krw_streams(monkeypatch, tmp_path):
    import junhyunbank.runtime_engine as module

    rows = [
        {"market": "KRW-BTC", "market_event": _normal_event()},
        {"market": "KRW-ETH", "market_event": _normal_event()},
        {
            "market": "KRW-RISK",
            "market_event": _normal_event(
                caution={"PRICE_FLUCTUATIONS": True}
            ),
        },
        {"market": "BTC-ETH", "market_event": _normal_event()},
    ]
    _FakeStream.instances.clear()
    monkeypatch.setattr(module, "MarketStream", _FakeStream)
    engine = _engine(tmp_path, _MarketClient(rows))

    engine._refresh_markets()

    assert engine._allowed_markets == ["KRW-BTC", "KRW-ETH"]
    assert len(engine._global_streams) == 1
    assert engine._global_streams[0].started == 1
    assert engine._global_streams[0].markets == ["KRW-BTC", "KRW-ETH"]
    universe = [e for e in engine.drain_events() if e.get("type") == "market_universe"]
    assert universe and universe[-1]["count"] == 2
    assert universe[-1]["flagged"] == 1


def test_refresh_markets_never_silently_accepts_zero_safe_markets(tmp_path):
    rows = [
        {
            "market": "KRW-BTC",
            "market_event": {"warning": {"unexpected": False}, "caution": {}},
        }
    ]
    client = _MarketClient(rows)
    engine = _engine(tmp_path, client)

    with pytest.raises(RuntimeError, match="안전하게 해석 가능한 종목이 0개"):
        engine._refresh_markets()

    # Immediate loop iterations must not hammer /v1/market/all again.
    engine._refresh_markets()
    assert client.calls == 1


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
