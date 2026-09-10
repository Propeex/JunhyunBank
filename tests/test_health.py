from junhyunbank.health import StrategyHealthGovernor


def test_health_starts_neutral_conservative():
    assert StrategyHealthGovernor().score([]) == 0.65


def test_persistent_negative_distribution_can_stop_entries():
    outcomes = [-1.0] * 20
    assert StrategyHealthGovernor().score(outcomes) == 0.0


def test_positive_recent_results_score_better_than_negative():
    governor = StrategyHealthGovernor()
    assert governor.score([0.8] * 15) > governor.score([-0.1] * 15)
