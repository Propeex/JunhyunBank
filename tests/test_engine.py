import time

import pytest

from junhyunbank.engine import TradingEngine as BaseTradingEngine
from junhyunbank.execution_model import (
    best_ioc_buy_capacity_krw,
    best_ioc_round_trip_capacity_krw,
    best_ioc_sell_capacity_krw,
)
from junhyunbank.live_engine import TradingEngine
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
    assert BaseTradingEngine._is_warning_market(
        {"market": "KRW-X", "market_event": {"warning": True}}
    )
    assert not BaseTradingEngine._is_warning_market(
        {"market": "KRW-X", "market_event": {"warning": False}}
    )


def test_best_ioc_buy_does_not_walk_to_worse_ask_levels():
    book = _book()
    slippage, fillable = TradingEngine._simulate_buy_slippage(book, 2500.0)

    assert slippage == 0.0
    assert fillable == pytest.approx(1000.0)  # 100 * 10 at best ask only
    assert best_ioc_buy_capacity_krw(book) == pytest.approx(1000.0)


def test_best_ioc_sell_does_not_walk_to_worse_bid_levels():
    book = _book()
    slippage, fillable = TradingEngine._simulate_sell_slippage(book, 2500.0)

    assert slippage == 0.0
    assert fillable == pytest.approx(990.0)  # 99 * 10 at best bid only
    assert best_ioc_sell_capacity_krw(book) == pytest.approx(990.0)


def test_live_round_trip_capacity_is_current_top_level_liquidity_not_edge_depth():
    book = _book()
    tight = TradingEngine._liquidity_capacity(
        book,
        expected_move_pct=0.011,
        bid_fee=0.0005,
        ask_fee=0.0005,
    )
    huge_edge = TradingEngine._liquidity_capacity(
        book,
        expected_move_pct=0.50,
        bid_fee=0.0005,
        ask_fee=0.0005,
    )

    assert tight == pytest.approx(990.0)
    assert huge_edge == pytest.approx(tight)
    assert best_ioc_round_trip_capacity_krw(book) == pytest.approx(tight)


def test_deeper_book_size_cannot_increase_best_ioc_capacity():
    book = _book()
    baseline = best_ioc_round_trip_capacity_krw(book)
    book.ask_sizes[1:] = [1_000_000.0] * 4
    book.bid_sizes[1:] = [1_000_000.0] * 4

    assert best_ioc_round_trip_capacity_krw(book) == pytest.approx(baseline)


def test_best_ioc_simulation_reports_unfilled_remainder_as_capacity_not_slippage():
    book = _book()
    buy_slip, buy_fillable = TradingEngine._simulate_buy_slippage(book, 1500.0)
    sell_slip, sell_fillable = TradingEngine._simulate_sell_slippage(book, 1500.0)

    assert buy_slip == sell_slip == 0.0
    assert buy_fillable < 1500.0
    assert sell_fillable < 1500.0
