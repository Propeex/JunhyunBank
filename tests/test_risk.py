from junhyunbank.config import RiskConfig
from junhyunbank.risk import RiskManager


def test_daily_loss_limit_blocks_new_position():
    risk = RiskManager(RiskConfig(daily_loss_limit_pct=0.03))
    risk.reset_session(1_000_000)
    result = risk.can_open(
        current_equity=960_000,
        available_cash=500_000,
        open_position_count=0,
    )
    assert not result.allowed


def test_emergency_blocks_new_position():
    risk = RiskManager(RiskConfig())
    risk.reset_session(1_000_000)
    risk.trigger_emergency()
    result = risk.can_open(
        current_equity=1_000_000,
        available_cash=1_000_000,
        open_position_count=0,
    )
    assert not result.allowed


def test_stop_loss_and_take_profit():
    risk = RiskManager(RiskConfig(stop_loss_pct=0.02, take_profit_pct=0.04))
    assert risk.exit_reason(avg_price=100, current_price=97) is not None
    assert risk.exit_reason(avg_price=100, current_price=105) is not None
