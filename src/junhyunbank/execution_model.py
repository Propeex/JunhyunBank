from __future__ import annotations

import math

from .strategy import BookSnapshot


def _valid_level(price: float, size: float) -> bool:
    return (
        math.isfinite(float(price))
        and math.isfinite(float(size))
        and float(price) > 0.0
        and float(size) >= 0.0
    )


def best_ioc_buy_capacity_krw(book: BookSnapshot) -> float:
    """Immediate quote-value capacity for a BUY `best + IOC` snapshot.

    Upbit `ord_type=best` sets the order price to the best opposing quote at
    order acceptance. With IOC, quantity that cannot execute at that limit (or
    better) is cancelled rather than walking to worse ask levels. Therefore the
    current snapshot capacity is the aggregate size at the best ask only.
    """
    if not book.ask_prices or not book.ask_sizes:
        return 0.0
    price, size = float(book.ask_prices[0]), float(book.ask_sizes[0])
    if not _valid_level(price, size):
        return 0.0
    return price * size


def best_ioc_sell_capacity_krw(book: BookSnapshot) -> float:
    """Immediate quote-value capacity for a SELL `best + IOC` snapshot."""
    if not book.bid_prices or not book.bid_sizes:
        return 0.0
    price, size = float(book.bid_prices[0]), float(book.bid_sizes[0])
    if not _valid_level(price, size):
        return 0.0
    return price * size


def best_ioc_round_trip_capacity_krw(book: BookSnapshot) -> float:
    """Conservative current size that fits both best ask and best bid.

    This is not an arbitrary portfolio cap. It is a snapshot liquidity cap so a
    new position is not deliberately sized larger than either side's currently
    executable best-price liquidity. The book can still change before matching,
    so real IOC partial fills remain possible and are handled by durable order
    reconciliation.
    """
    return min(best_ioc_buy_capacity_krw(book), best_ioc_sell_capacity_krw(book))


def simulate_best_ioc_buy(book: BookSnapshot, amount_krw: float) -> tuple[float, float]:
    """Return `(price_slippage, immediately_fillable_quote_value)`.

    The snapshot model has zero depth-walk slippage because a best order cannot
    execute at a worse price than the best opposing quote fixed at acceptance.
    Excess requested quote value is unfilled/cancelled by IOC instead.
    """
    requested = float(amount_krw)
    if not math.isfinite(requested) or requested <= 0.0:
        return 0.0, 0.0
    return 0.0, min(requested, best_ioc_buy_capacity_krw(book))


def simulate_best_ioc_sell(book: BookSnapshot, amount_krw: float) -> tuple[float, float]:
    """SELL equivalent of :func:`simulate_best_ioc_buy`."""
    requested = float(amount_krw)
    if not math.isfinite(requested) or requested <= 0.0:
        return 0.0, 0.0
    return 0.0, min(requested, best_ioc_sell_capacity_krw(book))
