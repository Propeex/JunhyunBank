from junhyunbank.config import SafetyConfig
from junhyunbank.risk import RiskManager


def test_dynamic_amount_has_no_fixed_cap():
    risk = RiskManager(SafetyConfig())
    result = risk.can_open(available_cash=1_000_000, amount_krw=900_000, min_order_krw=5_000, stream_age_seconds=0.1)
    assert result.allowed


def test_stale_market_data_blocks_entry():
    risk = RiskManager(SafetyConfig(market_data_stale_seconds=2.0))
    result = risk.can_open(available_cash=1_000_000, amount_krw=100_000, min_order_krw=5_000, stream_age_seconds=2.1)
    assert not result.allowed


def test_exchange_minimum_blocks_too_small_dynamic_order():
    risk = RiskManager(SafetyConfig())
    result = risk.can_open(available_cash=1_000_000, amount_krw=4_999, min_order_krw=5_000, stream_age_seconds=0.1)
    assert not result.allowed


def test_emergency_and_repeated_api_failures_block_entry():
    risk = RiskManager(SafetyConfig(max_api_failures=2))
    risk.report_api_failure(); risk.report_api_failure()
    assert not risk.can_open(available_cash=1_000_000, amount_krw=100_000, min_order_krw=5_000, stream_age_seconds=0.1).allowed
    risk.reset_session(); risk.trigger_emergency()
    assert not risk.can_open(available_cash=1_000_000, amount_krw=100_000, min_order_krw=5_000, stream_age_seconds=0.1).allowed
