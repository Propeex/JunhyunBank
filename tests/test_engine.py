import time

from junhyunbank.engine import TradingEngine
from junhyunbank.strategy import BookSnapshot


def _book():
    return BookSnapshot(
        received_at=time.monotonic(),
        bid_prices=[99.0, 98.0, 97.0, 96.0, 95.0],
        bid_sizes=[10.0] * 5,
        ask_prices=[100.0, 101.0, 102.0, 103.0, 104.0],
        ask_sizes=[10.0] * 5,
        imbalance=0.0,
        micro_bias=0.0,
        spread_pct=0.01,
    )


def test_warning_market_is_excluded():
    assert TradingEngine._is_warning_market({"market": "KRW-X", "market_event": {"warning": True}})
    assert not TradingEngine._is_warning_market({"market": "KRW-X", "market_event": {"warning": False}})


def test_buy_slippage_grows_when_order_walks_book():
    book = _book()
    small, small_fill = TradingEngine._simulate_buy_slippage(book, 500)
    large, large_fill = TradingEngine._simulate_buy_slippage(book, 2500)
    assert small_fill == 500
    assert large_fill == 2500
    assert large >= small


def test_liquidity_capacity_comes_from_expected_edge_not_fixed_krw_cap():
    book = _book()
    tight = TradingEngine._liquidity_capacity(book, expected_move_pct=0.011, bid_fee=0.0005, ask_fee=0.0005)
    wide = TradingEngine._liquidity_capacity(book, expected_move_pct=0.08, bid_fee=0.0005, ask_fee=0.0005)
    assert wide >= tight
    assert wide > 0
