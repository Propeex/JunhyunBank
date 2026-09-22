from types import SimpleNamespace

import pytest

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


def _budget_config(**changes):
    values = {
        "min_order_krw": 5_000.0,
        "cash_reserve_fraction": 0.15,
        "single_position_fraction": 0.15,
        "gross_exposure_fraction": 0.60,
        "per_trade_risk_fraction": 0.0025,
        "portfolio_risk_fraction": 0.01,
        "liquidity_participation_fraction": 0.10,
        "session_drawdown_halt_fraction": 0.02,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _entry_budget(risk, **changes):
    values = {
        "equity_krw": 1_000_000,
        "available_cash_krw": 1_000_000,
        "gross_exposure_krw": 0,
        "open_risk_krw": 0,
        "initial_risk_pct": 0.02,
        "liquidity_capacity_krw": 10_000_000,
        "signal_fraction": 1.0,
        "session_return_pct": 0.0,
        "min_order_krw": 5_000,
    }
    values.update(changes)
    return risk.entry_budget(**values)


def test_entry_budget_uses_planned_loss_not_available_cash_as_trade_size():
    risk = RiskManager(_budget_config())

    result = _entry_budget(risk)

    assert result.allowed
    assert result.limiting_factor == "per_trade_risk"
    assert result.amount_krw == pytest.approx(125_000)
    assert result.amount_krw * 0.02 == pytest.approx(2_500)


def test_entry_budget_blocks_when_gross_exposure_room_is_below_exchange_minimum():
    risk = RiskManager(_budget_config())

    result = _entry_budget(risk, gross_exposure_krw=596_000)

    assert not result.allowed
    assert result.limiting_factor == "gross_exposure"
    assert result.amount_krw == pytest.approx(4_000)


def test_entry_budget_counts_existing_open_risk_before_new_position():
    risk = RiskManager(_budget_config())

    result = _entry_budget(risk, open_risk_krw=9_950)

    assert not result.allowed
    assert result.limiting_factor == "portfolio_risk"
    assert result.amount_krw == pytest.approx(2_500)


def test_entry_budget_halts_at_session_drawdown_limit():
    risk = RiskManager(_budget_config())

    result = _entry_budget(risk, session_return_pct=-0.02)

    assert not result.allowed
    assert result.limiting_factor == "session_drawdown"
    assert result.amount_krw == 0


def test_entry_budget_rejects_non_finite_inputs():
    risk = RiskManager(_budget_config())

    result = _entry_budget(risk, liquidity_capacity_krw=float("nan"))

    assert not result.allowed
    assert result.amount_krw == 0
