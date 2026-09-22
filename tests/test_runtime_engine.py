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
    # Small fixtures opt out of the production absolute-size floor. Sentinel,
    # shrink and schema checks remain active in these tests.
    config.safety.market_universe_min_size = 1
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


def test_market_event_without_caution_fails_closed():
    row = {"market": "KRW-BTC", "market_event": {"warning": False}}

    flagged, understood = TradingEngine._market_alert_status(row)

    assert flagged is False
    assert understood is False
    snapshot = TradingEngine._classify_market_universe([row])
    assert snapshot["allowed"] == []
    assert snapshot["unknown_count"] == 1


def test_empty_caution_object_fails_closed():
    row = {
        "market": "KRW-BTC",
        "market_event": {"warning": False, "caution": {}},
    }

    flagged, understood = TradingEngine._market_alert_status(row)

    assert flagged is False
    assert understood is False
    assert TradingEngine._classify_market_universe([row])["allowed"] == []


@pytest.mark.parametrize(
    "event",
    [
        {"warning": None, "caution": {}},
        {"warning": False, "caution": {"PRICE_FLUCTUATIONS": None}},
    ],
)
def test_null_alert_values_fail_closed(event):
    row = {"market": "KRW-BTC", "market_event": event}

    flagged, understood = TradingEngine._market_alert_status(row)

    assert flagged is False
    assert understood is False
    snapshot = TradingEngine._classify_market_universe([row])
    assert snapshot["allowed"] == []
    assert snapshot["unknown_count"] == 1


def test_duplicate_market_uses_worst_case_alert_status():
    rows = [
        {"market": "KRW-BTC", "market_event": _normal_event()},
        {
            "market": "KRW-BTC",
            "market_event": _normal_event(
                caution={"PRICE_FLUCTUATIONS": True}
            ),
        },
        {"market": "KRW-ETH", "market_event": _normal_event()},
    ]

    snapshot = TradingEngine._classify_market_universe(rows)

    assert snapshot["raw_krw"] == ["KRW-BTC", "KRW-ETH"]
    assert snapshot["allowed"] == ["KRW-ETH"]
    assert snapshot["flagged_count"] == 1


def test_duplicate_unknown_row_cannot_be_overridden_by_normal_row():
    rows = [
        {"market": "KRW-BTC", "market_event": _normal_event()},
        {"market": "KRW-BTC", "market_event": {"warning": False}},
    ]

    snapshot = TradingEngine._classify_market_universe(rows)

    assert snapshot["allowed"] == []
    assert snapshot["flagged_count"] == 0
    assert snapshot["unknown_count"] == 1
    assert snapshot["unknown_samples"] == ["KRW-BTC"]


def test_initial_validation_rejects_conflicting_duplicate_btc_rows():
    safety = AppConfig().safety
    safety.market_universe_min_size = 1
    rows = [
        {"market": "KRW-BTC", "market_event": _normal_event()},
        {
            "market": "KRW-BTC",
            "market_event": _normal_event(warning=True),
        },
        {"market": "KRW-ETH", "market_event": _normal_event()},
    ]

    with pytest.raises(RuntimeError, match="KRW-BTC가 안전 허용목록"):
        TradingEngine.validated_initial_markets(rows, safety)


def test_refresh_markets_builds_nonzero_krw_streams(monkeypatch, tmp_path):
    # _restart_global_streams is inherited from the base engine and resolves
    # MarketStream in junhyunbank.engine, so patch that module for this test.
    import junhyunbank.engine as base_module

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
    monkeypatch.setattr(base_module, "MarketStream", _FakeStream)
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


def test_refresh_markets_rejects_missing_btc_sentinel(tmp_path):
    rows = [
        {"market": "KRW-ETH", "market_event": _normal_event()},
        {"market": "KRW-XRP", "market_event": _normal_event()},
    ]
    engine = _engine(tmp_path, _MarketClient(rows))

    with pytest.raises(RuntimeError, match="KRW-BTC 누락"):
        engine._refresh_markets()

    assert engine._market_discovery_ready is False
    assert engine._allowed_markets == []


def test_research_universe_uses_production_initial_sanity_floor():
    rows = [
        {"market": "KRW-BTC", "market_event": _normal_event()},
        {"market": "KRW-ETH", "market_event": _normal_event()},
    ]

    with pytest.raises(RuntimeError, match="최소 50개"):
        TradingEngine.validated_initial_markets(rows, AppConfig().safety)

    complete = [
        {"market": "KRW-BTC", "market_event": _normal_event()}
    ] + [
        {"market": f"KRW-T{index}", "market_event": _normal_event()}
        for index in range(49)
    ]
    assert len(
        TradingEngine.validated_initial_markets(complete, AppConfig().safety)
    ) == 50


def test_initial_universe_requires_safe_btc_and_enough_safe_markets():
    safety = AppConfig().safety
    rows = [
        {
            "market": "KRW-BTC",
            "market_event": _normal_event(
                caution={"PRICE_FLUCTUATIONS": True}
            ),
        }
    ] + [
        {"market": f"KRW-T{index}", "market_event": _normal_event()}
        for index in range(60)
    ]
    with pytest.raises(RuntimeError, match="KRW-BTC가 안전 허용목록"):
        TradingEngine.validated_initial_markets(rows, safety)

    too_few_safe = [
        {"market": "KRW-BTC", "market_event": _normal_event()}
    ] + [
        {"market": f"KRW-S{index}", "market_event": _normal_event()}
        for index in range(39)
    ] + [
        {
            "market": f"KRW-R{index}",
            "market_event": _normal_event(
                caution={"TRADING_VOLUME_SOARING": True}
            ),
        }
        for index in range(20)
    ]
    with pytest.raises(RuntimeError, match="안전 허용시장 40개"):
        TradingEngine.validated_initial_markets(too_few_safe, safety)


def test_refresh_markets_rejects_large_universe_shrink_without_replacing_old_set(
    monkeypatch, tmp_path
):
    import junhyunbank.engine as base_module

    initial = [
        {"market": "KRW-BTC", "market_event": _normal_event()}
    ] + [
        {"market": f"KRW-X{index:03d}", "market_event": _normal_event()}
        for index in range(99)
    ]
    client = _MarketClient(initial)
    _FakeStream.instances.clear()
    monkeypatch.setattr(base_module, "MarketStream", _FakeStream)
    engine = _engine(tmp_path, client)
    engine._refresh_markets()
    original = list(engine._allowed_markets)

    client.rows = initial[:20]
    with pytest.raises(RuntimeError, match="허용시장 급감"):
        engine._refresh_markets()

    assert engine._allowed_markets == original
    assert engine._market_discovery_ready is False


def test_refresh_markets_rejects_same_size_universe_replacement(
    monkeypatch, tmp_path
):
    """A same-sized but unrelated response is not healthy retention."""
    import junhyunbank.engine as base_module

    initial = [
        {"market": "KRW-BTC", "market_event": _normal_event()}
    ] + [
        {"market": f"KRW-OLD{index:03d}", "market_event": _normal_event()}
        for index in range(99)
    ]
    replacement = [
        {"market": "KRW-BTC", "market_event": _normal_event()}
    ] + [
        {"market": f"KRW-NEW{index:03d}", "market_event": _normal_event()}
        for index in range(99)
    ]
    client = _MarketClient(initial)
    _FakeStream.instances.clear()
    monkeypatch.setattr(base_module, "MarketStream", _FakeStream)
    engine = _engine(tmp_path, client)
    engine._refresh_markets()
    original = list(engine._allowed_markets)

    client.rows = replacement
    with pytest.raises(RuntimeError, match="허용시장 급감"):
        engine._refresh_markets()

    assert engine._allowed_markets == original
    assert engine._market_discovery_ready is False


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


def test_start_again_recreates_streams_with_unchanged_markets(monkeypatch,tmp_path):
    import junhyunbank.engine as module
    client=_MarketClient([{'market':'KRW-BTC','market_event':_normal_event()}])
    engine=_engine(tmp_path,client)
    monkeypatch.setattr(module,'MarketStream',_FakeStream)
    engine._refresh_markets()
    old=engine._global_streams[0]
    old.stop()
    engine._global_streams.clear()
    engine._deep_markets=['KRW-BTC']
    engine._live_portfolio=lambda: (10000,10000,10000,[])
    class Thread:
        def __init__(self,**kwargs): pass
        def is_alive(self): return False
        def start(self): pass
    monkeypatch.setattr(module.threading,'Thread',Thread)
    engine.start()
    engine._refresh_markets()
    assert engine._global_streams[0] is not old
    assert engine._global_streams[0].started == 1
    assert engine._deep_markets == []


def test_market_newly_flagged_cannot_remain_entry_candidate(tmp_path):
    engine=_engine(tmp_path)
    engine._allowed_markets=['KRW-BTC']
    engine._deep_markets=['KRW-RISK']
    assert 'KRW-RISK' not in engine._select_deep_markets([('KRW-RISK',99),('KRW-BTC',80)])


def test_warning_managed_market_keeps_trade_subscription(monkeypatch,tmp_path):
    import junhyunbank.engine as module
    engine=_engine(tmp_path)
    monkeypatch.setattr(module,'MarketStream',_FakeStream)
    engine.storage.mark_managed_position('KRW-RISK',managed_quantity=1)
    engine._allowed_markets=['KRW-BTC']
    engine._restart_global_streams()
    assert engine._global_streams[0].markets==['KRW-BTC','KRW-RISK']
