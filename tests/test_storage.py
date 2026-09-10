from junhyunbank.storage import Storage


def test_managed_positions_are_persisted(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.mark_managed_position("KRW-BTC")
    assert storage.managed_markets() == {"KRW-BTC"}
    storage.unmark_managed_position("KRW-BTC")
    assert storage.managed_markets() == set()
