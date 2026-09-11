import queue
import time

from junhyunbank.config import AppConfig
from junhyunbank.live_engine import TradingEngine


class _Storage:
    def __init__(self, managed=()):
        self._managed = set(managed)

    def managed_markets(self):
        return set(self._managed)


class _Strategy:
    def __init__(self, ages, scores):
        self.ages = dict(ages)
        self.scores = dict(scores)

    def trade_age(self, market):
        return float(self.ages.get(market, float("inf")))

    def hot_score(self, market):
        return float(self.scores.get(market, 0.0))


def _engine(*, ages, scores, managed=()):
    engine = TradingEngine.__new__(TradingEngine)
    engine.config = AppConfig()
    engine.config.safety.market_data_stale_seconds = 3.0
    engine.config.strategy.scanner_candidate_count = 4
    engine.config.strategy.deep_candidate_count = 2
    engine.config.strategy.deep_min_residency_seconds = 30.0
    engine.config.strategy.deep_switch_margin = 4.0
    engine.storage = _Storage(managed)
    engine.strategy = _Strategy(ages, scores)
    engine._allowed_markets = sorted(scores)
    engine._deep_markets = []
    engine._deep_entered_at = {}
    engine._actionable_ranked = ()
    engine._candidate_stale_skipped = 0
    engine._candidate_supplemented = 0
    engine._run_started_at = time.monotonic()
    engine._last_no_candidate_warning = 0.0
    engine.events = queue.Queue()
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
