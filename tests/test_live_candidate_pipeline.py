import time

from junhyunbank.config import AppConfig
from junhyunbank.engine import EventBuffer
from junhyunbank.live_engine import TradingEngine
from junhyunbank.strategy import MicroFlowStrategy


class _Storage:
    def __init__(self, managed=()):
        self._managed = set(managed)

    def managed_markets(self):
        return set(self._managed)


class _Strategy:
    def __init__(self, ages, scores, config):
        self.ages = dict(ages)
        self.scores = dict(scores)
        self.config = config

    def trade_age(self, market):
        return float(self.ages.get(market, float("inf")))

    def hot_score(self, market):
        return float(self.scores.get(market, 0.0))

    # Exercise the production selector with deterministic ages/scores instead
    # of duplicating its policy in this test fixture.
    select_actionable_deep_markets = (
        MicroFlowStrategy.select_actionable_deep_markets
    )


def _engine(*, ages, scores, managed=()):
    engine = TradingEngine.__new__(TradingEngine)
    engine.config = AppConfig()
    engine.config.safety.market_data_stale_seconds = 3.0
    engine.config.strategy.scanner_candidate_count = 4
    engine.config.strategy.deep_candidate_count = 2
    engine.config.strategy.deep_min_residency_seconds = 30.0
    engine.config.strategy.deep_switch_margin = 4.0
    engine.storage = _Storage(managed)
    engine.strategy = _Strategy(ages, scores, engine.config.strategy)
    engine._allowed_markets = sorted(scores)
    engine._deep_markets = []
    engine._deep_entered_at = {}
    engine._actionable_ranked = ()
    engine._candidate_stale_skipped = 0
    engine._candidate_supplemented = 0
    engine._run_started_at = time.monotonic()
    engine._last_no_candidate_warning = 0.0
    engine.events = EventBuffer()
    engine._reported_event_drops = 0
    engine._latest_prices = {}
    return engine


def test_stale_top_scores_do_not_starve_fresh_markets_below_scanner_cutoff():
    engine = _engine(
        ages={
            "KRW-A": 8.0,
            "KRW-B": 6.0,
            "KRW-C": 0.5,
            "KRW-D": 0.2,
            "KRW-E": 0.1,
            "KRW-F": 0.8,
        },
        scores={
            "KRW-A": 99.0,
            "KRW-B": 98.0,
            "KRW-C": 50.0,
            "KRW-D": 70.0,
            "KRW-E": 60.0,
            "KRW-F": 40.0,
        },
    )
    # Simulate the legacy scanner top-N: stale A/B displaced fresh D/E/F even
    # though those markets can actually pass the live 3-second entry gate.
    ranked = [("KRW-A", 99.0), ("KRW-B", 98.0), ("KRW-C", 50.0)]
    engine._deep_markets = ["KRW-A"]
    engine._deep_entered_at = {"KRW-A": time.monotonic()}

    selected = engine._select_deep_markets(ranked)

    assert selected == ["KRW-D", "KRW-E"]
    assert [market for market, _ in engine._actionable_ranked] == [
        "KRW-D",
        "KRW-E",
        "KRW-C",
        "KRW-F",
    ]
    assert engine._candidate_stale_skipped == 2
    assert engine._candidate_supplemented == 3
    assert "KRW-A" not in selected  # stale overrides the 30s anti-churn residency


def test_stale_managed_position_remains_in_deep_monitoring_for_exit_safety():
    engine = _engine(
        ages={"KRW-MANAGED": 100.0, "KRW-FRESH": 0.1},
        scores={"KRW-MANAGED": 90.0, "KRW-FRESH": 80.0},
        managed={"KRW-MANAGED"},
    )
    engine._deep_markets = ["KRW-MANAGED"]
    engine._deep_entered_at = {"KRW-MANAGED": time.monotonic()}

    selected = engine._select_deep_markets([("KRW-FRESH", 80.0)])

    assert "KRW-MANAGED" in selected
    assert "KRW-FRESH" in selected


def test_expired_incumbent_is_replaced_only_after_switch_margin_is_cleared():
    engine = _engine(
        ages={"KRW-A": 0.1, "KRW-B": 0.1},
        scores={"KRW-A": 70.0, "KRW-B": 72.0},
    )
    engine.config.strategy.deep_candidate_count = 1
    engine._deep_markets = ["KRW-A"]
    engine._deep_entered_at = {
        "KRW-A": time.monotonic() - 31.0,
    }

    selected = engine._select_deep_markets(
        [("KRW-B", 72.0), ("KRW-A", 70.0)]
    )
    assert selected == ["KRW-A"]

    engine.strategy.scores["KRW-B"] = 75.0
    selected = engine._select_deep_markets(
        [("KRW-B", 75.0), ("KRW-A", 70.0)]
    )
    assert selected == ["KRW-B"]


def test_candidate_ui_event_is_rewritten_to_actionable_fresh_ranking():
    engine = _engine(
        ages={"KRW-A": 0.1, "KRW-B": 0.2},
        scores={"KRW-A": 80.0, "KRW-B": 70.0},
    )
    engine._actionable_ranked = (("KRW-B", 70.0),)
    engine._candidate_stale_skipped = 3
    engine._candidate_supplemented = 1
    engine._latest_prices = {"KRW-B": 123.0}
    engine.events.put(
        {
            "type": "candidates",
            "markets": ["KRW-A"],
            "scores": {"KRW-A": 99.0},
            "prices": {"KRW-A": 1.0},
            "tracked": 2,
        }
    )

    event = engine.drain_events()[0]

    assert event["markets"] == ["KRW-B"]
    assert event["scores"] == {"KRW-B": 70.0}
    assert event["prices"] == {"KRW-B": 123.0}
    assert event["stale_skipped"] == 3
    assert event["supplemented"] == 1


def test_mature_fresh_incumbent_survives_below_margin_challenger():
    engine = _engine(ages={'KRW-A': .1, 'KRW-B': .1}, scores={'KRW-A': 80, 'KRW-B': 81})
    engine.config.strategy.deep_candidate_count = 1
    engine._deep_markets = ['KRW-A']
    engine._deep_entered_at = {'KRW-A': time.monotonic() - 31}
    assert engine._select_deep_markets([('KRW-B', 81), ('KRW-A', 80)]) == ['KRW-A']


def test_challenger_must_wait_for_residency_then_meet_margin():
    engine = _engine(ages={'KRW-A': .1, 'KRW-B': .1}, scores={'KRW-A': 80, 'KRW-B': 84})
    engine.config.strategy.deep_candidate_count = 1
    engine._deep_markets = ['KRW-A']
    engine._deep_entered_at = {'KRW-A': time.monotonic() - 5}
    ranked = [('KRW-B', 84), ('KRW-A', 80)]
    assert engine._select_deep_markets(ranked) == ['KRW-A']
    engine._deep_entered_at['KRW-A'] = time.monotonic() - 31
    assert engine._select_deep_markets(ranked) == ['KRW-B']


def test_incumbent_outside_truncated_ranking_uses_its_actual_score():
    engine = _engine(ages={'KRW-A': .1, 'KRW-B': .1}, scores={'KRW-A': 80, 'KRW-B': 81})
    engine.config.strategy.scanner_candidate_count = 1
    engine.config.strategy.deep_candidate_count = 1
    engine._deep_markets = ['KRW-A']
    engine._deep_entered_at = {'KRW-A': time.monotonic() - 31}
    assert engine._select_deep_markets([('KRW-B', 81)]) == ['KRW-A']


def test_idle_incumbent_uses_only_spare_capacity_and_expires():
    engine = _engine(ages={'KRW-IDLE': 4, 'KRW-FRESH': .1}, scores={'KRW-IDLE': 99, 'KRW-FRESH': 70})
    engine._deep_markets = ['KRW-IDLE']
    engine._deep_entered_at = {'KRW-IDLE': time.monotonic()}
    ranked = [('KRW-FRESH', 70)]
    assert engine._select_deep_markets(ranked) == ['KRW-FRESH', 'KRW-IDLE']
    assert engine._actionable_ranked == (('KRW-FRESH', 70),)
    engine.strategy.ages['KRW-IDLE'] = 31
    assert engine._select_deep_markets(ranked) == ['KRW-FRESH']


def test_fresh_candidate_immediately_takes_idle_slot_regardless_of_residency():
    engine = _engine(ages={'KRW-IDLE': 4, 'KRW-FRESH': .1}, scores={'KRW-IDLE': 99, 'KRW-FRESH': 70})
    engine.config.strategy.deep_candidate_count = 1
    engine._deep_markets = ['KRW-IDLE']
    engine._deep_entered_at = {'KRW-IDLE': time.monotonic()}
    assert engine._select_deep_markets([('KRW-FRESH', 70)]) == ['KRW-FRESH']


def test_shrinking_deep_limit_bounds_nonmanaged_slots_and_keeps_managed():
    engine = _engine(
        ages={'KRW-A': .1, 'KRW-B': .1, 'KRW-C': .1, 'KRW-D': 4, 'KRW-M': 100},
        scores={'KRW-A': 70, 'KRW-B': 90, 'KRW-C': 80, 'KRW-D': 99, 'KRW-M': 10},
        managed={'KRW-M'},
    )
    engine._deep_markets = ['KRW-A', 'KRW-B', 'KRW-C', 'KRW-D', 'KRW-M']
    engine._deep_entered_at = {market: time.monotonic() for market in engine._deep_markets}
    selected = engine._select_deep_markets([('KRW-B', 90), ('KRW-C', 80), ('KRW-A', 70)])
    assert selected == ['KRW-B', 'KRW-C', 'KRW-M']


def test_short_trade_lull_preserves_actual_subscription_and_book_history(monkeypatch):
    import junhyunbank.runtime_engine as runtime
    from junhyunbank.strategy import MicroFlowStrategy

    class FakeStream:
        def __init__(self, markets, **kwargs):
            self.markets = markets
            self.updates = []
            self.stopped = False

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def update_markets(self, markets):
            self.updates.append(markets)

    monkeypatch.setattr(runtime, 'MarketStream', FakeStream)
    engine = _engine(ages={}, scores={'KRW-X': 90})
    engine.strategy = MicroFlowStrategy(engine.config.strategy)
    engine._deep_stream = None
    engine._restart_deep_stream(['KRW-X'])
    stream = engine._deep_stream
    history = [(second, .5, .0001) for second in range(12)]
    engine.strategy._book_history['KRW-X'].extend(history)
    engine.strategy._last_trade_received['KRW-X'] = time.monotonic() - 4

    engine._restart_deep_stream(engine._select_deep_markets([]))
    assert engine._actionable_ranked == ()
    assert engine._deep_stream is stream and not stream.stopped and stream.updates == []
    assert list(engine.strategy._book_history['KRW-X']) == history

    engine.strategy._last_trade_received['KRW-X'] = time.monotonic()
    engine._restart_deep_stream(engine._select_deep_markets([('KRW-X', 90)]))
    assert engine._actionable_ranked == (('KRW-X', 90),)
    assert engine._deep_stream is stream and stream.updates == []
    assert list(engine.strategy._book_history['KRW-X']) == history


def test_retained_idle_market_cannot_submit_stale_buy(tmp_path, monkeypatch):
    from junhyunbank.models import EngineState, Signal
    from junhyunbank.storage import Storage
    from test_entry_pipeline import Exchange, allow_fixture_regime, feed

    client = Exchange()
    engine = TradingEngine(client, storage=Storage(tmp_path / 'idle.db'))
    engine._state = EngineState.RUNNING
    engine._allowed_markets = ['KRW-X']
    engine._deep_markets = ['KRW-X']
    engine._deep_entered_at = {'KRW-X': time.monotonic() - 5}
    feed(engine.strategy)
    allow_fixture_regime(engine)
    assert engine.strategy.evaluate_entry('KRW-X', bid_fee=.0005, ask_fee=.0005,
                                          health=1, regime_factor=1).signal == Signal.BUY
    monkeypatch.setattr(engine.strategy, 'trade_age', lambda market: 4)
    engine._deep_markets = engine._select_deep_markets([])
    assert engine._deep_markets == ['KRW-X'] and engine._actionable_ranked == ()

    engine._evaluate_cycle()

    assert client.orders == [] and engine.storage.pending_orders() == []
    assert any('지연' in event.get('reason', '') for event in engine.drain_events())
