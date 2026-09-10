from junhyunbank.storage import Storage


def test_managed_positions_are_persisted_with_v2_state(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.mark_managed_position("KRW-BTC", entry_price=100.0, initial_risk_pct=0.02, managed_quantity=1.5, signal_kind="IGNITION")
    assert storage.managed_markets() == {"KRW-BTC"}
    state = storage.get_managed_state("KRW-BTC")
    assert state["entry_price"] == 100.0
    assert state["managed_quantity"] == 1.5
    storage.unmark_managed_position("KRW-BTC")
    assert storage.managed_markets() == set()


def test_strategy_outcomes_are_persisted(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.record_outcome(market="KRW-BTC", signal_kind="IGNITION", net_return_pct=0.01, normalized_return=0.5)
    assert storage.strategy_outcomes(10) == [0.5]
