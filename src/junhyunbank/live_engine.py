from __future__ import annotations

from .execution_model import (
    best_ioc_round_trip_capacity_krw,
    simulate_best_ioc_buy,
    simulate_best_ioc_sell,
)
from .runtime_engine import TradingEngine as RuntimeTradingEngine
from .strategy import BookSnapshot


class TradingEngine(RuntimeTradingEngine):
    """Production engine with exchange-order semantics kept explicit.

    The base engine historically estimated multi-level depth walking. JunhyunBank
    actually submits Upbit `best + IOC`, whose limit is the best opposing quote
    at acceptance; unfilled remainder is cancelled instead of walking to worse
    levels. Override only the pretrade liquidity helpers here so the live order
    state machine, runtime hardening, and strategy remain unchanged.
    """

    @staticmethod
    def _simulate_buy_slippage(
        book: BookSnapshot, amount_krw: float
    ) -> tuple[float, float]:
        return simulate_best_ioc_buy(book, amount_krw)

    @staticmethod
    def _simulate_sell_slippage(
        book: BookSnapshot, amount_krw: float
    ) -> tuple[float, float]:
        return simulate_best_ioc_sell(book, amount_krw)

    @staticmethod
    def _liquidity_capacity(
        book: BookSnapshot,
        *,
        expected_move_pct: float,
        bid_fee: float,
        ask_fee: float,
    ) -> float:
        # Expected edge can decide whether a trade is worth attempting, but it
        # cannot make a best+IOC order reach worse price levels. Keep the legacy
        # signature because `_evaluate_cycle()` calls it through polymorphism.
        _ = expected_move_pct, bid_fee, ask_fee
        return best_ioc_round_trip_capacity_krw(book)
