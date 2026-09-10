from junhyunbank.config import StrategyConfig
from junhyunbank.models import Candle, Signal
from junhyunbank.strategy import TrendRSIStrategy


def _candles(closes):
    return [
        Candle(
            market="KRW-TEST",
            timestamp=str(i),
            open=value,
            high=value,
            low=value,
            close=value,
            volume=1.0,
            trade_value=1.0,
        )
        for i, value in enumerate(closes)
    ]


def test_strategy_waits_for_enough_data():
    strategy = TrendRSIStrategy(StrategyConfig())
    result = strategy.evaluate(_candles([100.0] * 10))
    assert result.signal == Signal.HOLD


def test_strategy_sells_when_fast_trend_below_slow():
    config = StrategyConfig(fast_ma=3, slow_ma=5, rsi_period=3)
    strategy = TrendRSIStrategy(config)
    result = strategy.evaluate(_candles([110, 108, 106, 104, 102, 100, 98]))
    assert result.signal == Signal.SELL
