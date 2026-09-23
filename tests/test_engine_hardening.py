import time
from types import SimpleNamespace

from junhyunbank.engine import EventBuffer, ExecutableQuote
from junhyunbank.live_engine import TradingEngine
from junhyunbank.models import EngineState, Position, Signal
from junhyunbank.storage import Storage


def _quote(price: float, generation: float, *, bid_size: float = 100.0) -> ExecutableQuote:
    return ExecutableQuote(
        received_at=generation,
        bid_prices=(price,),
        bid_sizes=(bid_size,),
        ask_price=price * 1.001,
        spread_pct=0.001,
        source="test orderbook",
    )


class ManagedClient:
    access_key = "test"
    secret_key = "test"

    def __init__(self, *, with_position=True, quote_age=0.0):
        self.with_position = with_position
        self.quote_age = quote_age
        self.orders = []
        self.order_chance_calls = []

    def get_accounts(self):
        rows = [{"currency": "KRW", "balance": "100000", "locked": "0"}]
        if self.with_position:
            rows.append(
                {
                    "currency": "X",
                    "unit_currency": "KRW",
                    "balance": "1",
                    "locked": "0",
                    "avg_buy_price": "10000",
                }
            )
        return rows

    def get_orderbooks(self, markets):
        return [
            {
                "market": market,
                "timestamp": int((time.time() - self.quote_age) * 1000),
                "orderbook_units": [
                    {
                        "bid_price": 10500.0,
                        "ask_price": 10600.0,
                        "bid_size": 100.0,
                        "ask_size": 100.0,
                    }
                ],
            }
            for market in markets
        ]

    def get_tickers(self, markets):
        return [{"market": market, "trade_price": 10000.0} for market in markets]

    def get_order_chance(self, market):
        self.order_chance_calls.append(market)
        return {
            "bid_fee": "0.0005",
            "ask_fee": "0.0005",
            "market": {
                "bid": {"min_total": "5000"},
                "ask": {"min_total": "50"},
            },
        }

    def place_best_ioc_sell(self, market, quantity, *, identifier):
        self.orders.append((market, quantity, identifier))
        return {"uuid": "sell-1"}

    def get_order(self, **kwargs):
        return {
            "uuid": "sell-1",
            "market": "KRW-X",
            "side": "ask",
            "state": "done",
            "executed_volume": "1",
            "paid_fee": "5.25",
            "trades": [
                {"price": "10500", "volume": "1", "funds": "10500"}
            ],
        }


def test_ui_event_buffer_coalesces_prices_and_never_grows_unbounded():
    events = EventBuffer(max_items=100)
    for price in range(1_000):
        events.put({"type": "price", "market": "KRW-X", "price": price})
    assert events.qsize() == 1
    assert events.get_nowait()["price"] == 999

    for index in range(250):
        events.put({"type": "warning", "message": str(index)})
    assert events.qsize() == 100
    assert events.dropped == 150


def _managed_engine(tmp_path, client):
    engine = TradingEngine(client, storage=Storage(tmp_path / "engine.db"))
    engine._state = EngineState.RUNNING
    engine._allowed_markets = []
    engine._deep_markets = []
    engine.storage.mark_managed_position(
        "KRW-X",
        entry_price=10000.0,
        entry_amount_krw=10000.0,
        entry_fee_rate=0.0005,
        initial_risk_pct=0.05,
        managed_quantity=1.0,
        peak_price=12000.0,
    )
    engine.storage.update_managed_trailing_stop("KRW-X", 11000.0)
    return engine


def test_rejected_trade_cannot_overwrite_engine_price_cache(tmp_path, monkeypatch):
    engine = TradingEngine(ManagedClient(), storage=Storage(tmp_path / "price.db"))
    monkeypatch.setattr(engine.strategy, "on_trade", lambda event: False)

    engine._on_trade({"code": "KRW-X", "trade_price": 999.0, "timestamp": 1})

    assert "KRW-X" not in engine._latest_prices


def test_fresh_rest_best_bid_can_execute_persisted_trailing_stop(tmp_path):
    client = ManagedClient()
    engine = _managed_engine(tmp_path, client)

    engine._evaluate_cycle()

    assert len(client.orders) == 1
    assert client.order_chance_calls == []
    assert not engine.storage.managed_markets()
    diagnostics = [
        event for event in engine.drain_events() if event.get("type") == "position_diagnostic"
    ]
    assert diagnostics[-1]["quote_source"] == "REST orderbook"


def test_sell_is_cancelled_when_final_executable_bid_recovers(tmp_path, monkeypatch):
    client = ManagedClient()
    engine = _managed_engine(tmp_path, client)
    quotes = iter(
        [
            _quote(10500.0, 1.0),
            _quote(11500.0, 2.0),
            # The post-management strategy-PnL mark uses the same recovered
            # executable quote once more before entries can be considered.
            _quote(11500.0, 2.0),
        ]
    )
    monkeypatch.setattr(engine, "_fresh_executable_quote", lambda _: next(quotes))

    engine._evaluate_cycle()

    assert client.orders == []
    assert engine.storage.managed_markets() == {"KRW-X"}
    assert any(
        "청산 직전 재확인 해제" in event.get("reason", "")
        for event in engine.drain_events()
    )


def test_sell_is_cancelled_when_bid_recovers_at_post_boundary(tmp_path, monkeypatch):
    client = ManagedClient()
    engine = _managed_engine(tmp_path, client)
    quotes = iter(
        [
            _quote(10500.0, 1.0),  # initial decision: below persisted stop
            _quote(10500.0, 1.0),  # final decision still says SELL
            _quote(11500.0, 2.0),  # serialized pre-POST policy check recovers
            _quote(11500.0, 2.0),  # post-management strategy-PnL snapshot
        ]
    )
    monkeypatch.setattr(engine, "_fresh_executable_quote", lambda _: next(quotes))

    engine._evaluate_cycle()

    assert client.orders == []
    assert engine.storage.pending_orders() == []
    assert engine.storage.managed_markets() == {"KRW-X"}
    assert any(
        "POST 직전 청산 재확인 해제" in event.get("reason", "")
        for event in engine.drain_events()
    )


def test_stale_rest_quote_never_triggers_exit_from_old_price(tmp_path):
    client = ManagedClient(quote_age=30.0)
    engine = _managed_engine(tmp_path, client)

    engine._evaluate_cycle()

    assert client.orders == []
    assert engine.storage.managed_markets() == {"KRW-X"}
    assert any(
        "최신 매수호가 없음" in event.get("reason", "")
        for event in engine.drain_events()
    )


def test_rest_quote_age_includes_response_transport_delay(tmp_path):
    client = ManagedClient()

    def delayed_orderbook(markets):
        timestamp = int(time.time() * 1000)
        time.sleep(0.05)
        return [
            {
                "market": market,
                "timestamp": timestamp,
                "orderbook_units": [
                    {
                        "bid_price": 10500.0,
                        "ask_price": 10600.0,
                        "bid_size": 100.0,
                        "ask_size": 100.0,
                    }
                ],
            }
            for market in markets
        ]

    client.get_orderbooks = delayed_orderbook
    engine = TradingEngine(client, storage=Storage(tmp_path / "slow-quote.db"))
    engine.config.safety.market_data_stale_seconds = 0.01

    engine._refresh_managed_fallback_quotes(["KRW-X"])

    assert "KRW-X" not in engine._fallback_best_bids


def test_accepted_rest_quote_keeps_exchange_age_until_expiry(tmp_path, monkeypatch):
    client = ManagedClient()
    wall_now = 1_700_000_000.0
    monotonic_now = [500.0]
    exchange_age = 2.8

    def near_expiry_orderbook(markets):
        return [
            {
                "market": market,
                "timestamp": int((wall_now - exchange_age) * 1000),
                "orderbook_units": [
                    {
                        "bid_price": 10500.0,
                        "ask_price": 10600.0,
                        "bid_size": 100.0,
                        "ask_size": 100.0,
                    }
                ],
            }
            for market in markets
        ]

    client.get_orderbooks = near_expiry_orderbook
    monkeypatch.setattr("junhyunbank.engine.time.time", lambda: wall_now)
    monkeypatch.setattr(
        "junhyunbank.engine.time.monotonic", lambda: monotonic_now[0]
    )
    engine = TradingEngine(client, storage=Storage(tmp_path / "near-expiry.db"))
    engine.config.safety.market_data_stale_seconds = 3.0

    engine._refresh_managed_fallback_quotes(["KRW-X"])

    quote = engine._fallback_best_bids["KRW-X"]
    assert abs(quote.received_at - (monotonic_now[0] - exchange_age)) < 1e-6
    assert engine._fresh_executable_quote("KRW-X") is quote
    monotonic_now[0] += 0.3
    assert engine._fresh_executable_quote("KRW-X") is None


def test_missing_durable_entry_price_is_not_inferred_from_account_average(tmp_path):
    client = ManagedClient()
    engine = TradingEngine(client, storage=Storage(tmp_path / "missing-entry.db"))
    engine._state = EngineState.RUNNING
    engine.storage.mark_managed_position(
        "KRW-X",
        entry_fee_rate=0.0005,
        initial_risk_pct=0.05,
        managed_quantity=1.0,
        peak_price=12000.0,
    )
    engine.storage.update_managed_trailing_stop("KRW-X", 11000.0)

    engine._evaluate_cycle()

    assert client.orders == []
    assert engine.storage.managed_markets() == {"KRW-X"}
    assert engine.storage.get_managed_state("KRW-X")["status"] == "QUARANTINED"
    assert any("계정 평균단가로 추정하지 않고 격리" in event.get("reason", "")
               for event in engine.drain_events())


def test_repeated_missing_balance_quarantines_without_deleting_or_reviving(tmp_path):
    client = ManagedClient(with_position=False)
    engine = _managed_engine(tmp_path, client)
    engine.config.safety.settlement_grace_seconds = 0.0
    engine.config.safety.managed_absence_confirmations = 3

    for _ in range(3):
        engine._evaluate_cycle()

    state = engine.storage.get_managed_state("KRW-X")
    assert state["status"] == "QUARANTINED"
    assert state["absence_count"] == 3
    assert engine.storage.managed_markets() == {"KRW-X"}

    client.with_position = True
    engine._evaluate_cycle()
    assert engine.storage.get_managed_state("KRW-X")["status"] == "QUARANTINED"
    assert client.orders == []


def test_quantity_shrink_is_quarantined_before_managed_sell(tmp_path):
    client = ManagedClient()
    engine = _managed_engine(tmp_path, client)
    engine.storage.mark_managed_position("KRW-X", managed_quantity=2.0)

    engine._evaluate_cycle()

    state = engine.storage.get_managed_state("KRW-X")
    assert state["managed_quantity"] == 2.0
    assert state["status"] == "QUARANTINED"
    assert client.orders == []
    assert engine.storage.pending_orders() == []
    assert any(
        "계정 총수량이 영속 관리수량보다 작아" in event.get("reason", "")
        for event in engine.drain_events()
    )


def test_global_stream_gate_requires_every_live_partition_fresh(tmp_path):
    engine = TradingEngine(ManagedClient(), storage=Storage(tmp_path / "stream.db"))
    engine._global_streams = [
        SimpleNamespace(connected=True, age_seconds=0.1),
        SimpleNamespace(connected=False, age_seconds=0.1),
    ]
    assert engine._global_market_data_ready() is False
    engine._global_streams[1] = SimpleNamespace(connected=True, age_seconds=0.2)
    assert engine._global_market_data_ready() is True


def test_public_reconnect_invalidates_affected_strategy_continuity(tmp_path, monkeypatch):
    engine = TradingEngine(ManagedClient(), storage=Storage(tmp_path / "reset.db"))
    reset_trades = []
    reset_books = []
    monkeypatch.setattr(engine.strategy, "reset_trades", lambda markets: reset_trades.extend(markets))
    monkeypatch.setattr(engine.strategy, "reset_orderbook", lambda market: reset_books.append(market))

    engine._stream_status(
        {
            "state": "reconnecting",
            "name": "upbit-trades-1",
            "market_codes": ["KRW-A", "KRW-B"],
        }
    )
    engine._stream_status(
        {
            "state": "connecting",
            "name": "upbit-orderbook-deep",
            "market_codes": ["KRW-A"],
        }
    )

    assert reset_trades == ["KRW-A", "KRW-B"]
    assert reset_books == ["KRW-A"]


def test_entry_fee_lookups_are_bounded_per_cycle(tmp_path, monkeypatch):
    client = ManagedClient(with_position=False)
    engine = TradingEngine(client, storage=Storage(tmp_path / "fees.db"))
    engine._state = EngineState.RUNNING
    markets = [f"KRW-{letter}" for letter in "ABCDE"]
    engine._allowed_markets = markets
    engine._deep_markets = markets
    engine.config.strategy.max_fee_lookups_per_cycle = 2

    monkeypatch.setattr(engine.strategy, "market_regime", lambda _: ("NEUTRAL", 1.0))
    monkeypatch.setattr(
        engine.strategy,
        "rank_markets",
        lambda universe, limit: [(market, 1.0) for market in universe[:limit]],
    )
    monkeypatch.setattr(engine.strategy, "trade_age", lambda _: 0.0)
    monkeypatch.setattr(engine.strategy, "book_age", lambda _: 0.0)
    monkeypatch.setattr(
        engine.strategy,
        "evaluate_entry",
        lambda *args, **kwargs: SimpleNamespace(
            signal=Signal.HOLD,
            reason="test hold",
            score=0.0,
            expected_move_pct=0.0,
            round_trip_cost_pct=0.0,
        ),
    )

    engine._evaluate_cycle()

    assert client.order_chance_calls == markets[:2]
    assert any(
        "다음 평가 주기로 이월" in event.get("reason", "")
        for event in engine.drain_events()
    )


def test_bot_session_loss_is_not_masked_by_external_account_deposit(tmp_path):
    engine = TradingEngine(
        ManagedClient(with_position=False),
        storage=Storage(tmp_path / "strategy-pnl.db"),
    )
    engine._session_start_equity = 100_000.0
    engine._session_strategy_pnl_start = 0.0
    engine.storage.record_outcome(
        market="KRW-X",
        signal_kind="IGNITION",
        net_return_pct=-0.025,
        normalized_return=-1.0,
        net_pnl_krw=-2_500.0,
    )

    # The current account can be much larger after a user deposit; the risk
    # return remains tied to strategy PnL over the original session capital.
    session_return = engine._strategy_session_return([], equity_krw=1_000_000.0)

    assert session_return == -0.025
    budget = engine.risk.entry_budget(
        equity_krw=1_000_000.0,
        available_cash_krw=1_000_000.0,
        gross_exposure_krw=0.0,
        open_risk_krw=0.0,
        initial_risk_pct=0.02,
        liquidity_capacity_krw=1_000_000.0,
        signal_fraction=0.1,
        session_return_pct=session_return,
        min_order_krw=5_000.0,
    )
    assert budget.allowed is False
    assert budget.limiting_factor == "session_drawdown"


def test_zero_starting_equity_does_not_rebase_to_later_deposit(tmp_path):
    engine = TradingEngine(
        ManagedClient(with_position=False),
        storage=Storage(tmp_path / "zero-session-capital.db"),
    )
    engine._session_start_equity = 0.0
    engine._session_strategy_pnl_start = 0.0

    assert engine._strategy_session_return([], equity_krw=1_000_000.0) is None
    assert engine._session_start_equity == 0.0


def test_quarantined_lot_cannot_be_used_for_strategy_pnl(tmp_path):
    engine = TradingEngine(
        ManagedClient(),
        storage=Storage(tmp_path / "quarantined-pnl.db"),
    )
    engine.storage.mark_managed_position(
        "KRW-X",
        entry_price=10000.0,
        entry_amount_krw=10000.0,
        entry_fee_rate=0.0005,
        managed_quantity=1.0,
    )
    for _ in range(3):
        engine.storage.record_managed_absence("KRW-X", quarantine_after=3)
    engine._fallback_best_bids["KRW-X"] = _quote(
        10500.0, time.monotonic()
    )

    assert engine._strategy_pnl_snapshot(
        [Position("KRW-X", 1.0, 10000.0)]
    ) is None


def test_strategy_pnl_requires_executable_bid_not_last_trade(tmp_path):
    engine = TradingEngine(
        ManagedClient(),
        storage=Storage(tmp_path / "executable-baseline.db"),
    )
    engine.storage.mark_managed_position(
        "KRW-X",
        entry_price=10000.0,
        entry_fee_rate=0.0005,
        managed_quantity=1.0,
    )
    engine._latest_prices["KRW-X"] = 11000.0

    assert engine._strategy_pnl_snapshot(
        [Position("KRW-X", 1.0, 10000.0)]
    ) is None


def test_strategy_pnl_requires_best_ioc_depth_for_entire_managed_lot(tmp_path):
    engine = TradingEngine(
        ManagedClient(),
        storage=Storage(tmp_path / "pnl-depth.db"),
    )
    engine.storage.mark_managed_position(
        "KRW-X",
        entry_price=10000.0,
        entry_amount_krw=10000.0,
        entry_fee_rate=0.0005,
        managed_quantity=1.0,
    )
    position = [Position("KRW-X", 1.0, 10000.0)]
    engine._fallback_best_bids["KRW-X"] = _quote(
        10500.0, time.monotonic(), bid_size=0.5
    )

    assert engine._strategy_pnl_snapshot(position) is None

    engine._fallback_best_bids["KRW-X"] = _quote(
        10500.0, time.monotonic(), bid_size=1.0
    )
    assert engine._strategy_pnl_snapshot(position) is not None


def test_changed_quote_generation_during_final_guard_blocks_sell(tmp_path, monkeypatch):
    client = ManagedClient()
    engine = _managed_engine(tmp_path, client)
    quotes = iter(
        [
            _quote(10500.0, 1.0),  # initial decision
            _quote(10500.0, 1.0),  # final decision
            _quote(10500.0, 1.0),  # serialized guard decision
            _quote(10500.0, 2.0),  # generation changed during decision
            _quote(10500.0, 2.0),  # strategy-PnL mark
        ]
    )
    monkeypatch.setattr(engine, "_fresh_executable_quote", lambda _: next(quotes))

    engine._evaluate_cycle()

    assert client.orders == []
    assert engine.storage.managed_markets() == {"KRW-X"}
    assert any("호가 세대가 변경" in event.get("reason", "")
               for event in engine.drain_events())


def test_corrupt_managed_accounting_is_quarantined_before_sell(tmp_path):
    client = ManagedClient()
    engine = _managed_engine(tmp_path, client)
    engine.storage.mark_managed_position("KRW-X", entry_amount_krw=0.0)

    engine._evaluate_cycle()

    assert client.orders == []
    assert engine.storage.get_managed_state("KRW-X")["status"] == "QUARANTINED"
    assert any("회계 필드가 손상" in event.get("reason", "")
               for event in engine.drain_events())


def test_start_reconciles_terminal_sell_before_building_baseline(tmp_path, monkeypatch):
    client = ManagedClient(with_position=False)
    engine = TradingEngine(client, storage=Storage(tmp_path / "restart-reconcile.db"))
    engine.storage.mark_managed_position(
        "KRW-X",
        entry_price=10000.0,
        entry_amount_krw=10000.0,
        entry_fee_rate=0.0005,
        initial_risk_pct=0.05,
        managed_quantity=1.0,
    )
    engine.storage.create_order_intent("pending-sell", "KRW-X", "SELL", {})
    engine.storage.mark_order_submitted("pending-sell")
    monkeypatch.setattr(engine, "_run", lambda: None)

    engine.start()
    engine.wait()

    assert engine.storage.pending_orders() == []
    assert engine.storage.managed_markets() == set()
    assert engine._session_entry_locked is False


def test_start_keeps_exit_engine_alive_but_permanently_locks_entries_when_baseline_fails(
    tmp_path, monkeypatch
):
    class FailingAccountClient(ManagedClient):
        def get_accounts(self):
            raise RuntimeError("temporary account failure")

    engine = TradingEngine(
        FailingAccountClient(with_position=False),
        storage=Storage(tmp_path / "locked-start.db"),
    )
    monkeypatch.setattr(engine, "_run", lambda: None)

    engine.start()
    engine.wait()

    assert engine._state == EngineState.RUNNING
    assert engine._session_entry_locked is True
    assert engine.accepting_entries is False
    assert any("신규 매수는 영구 잠금" in event.get("message", "")
               for event in engine.drain_events())


def test_start_locks_entries_when_submitted_buy_cannot_be_reconciled(
    tmp_path, monkeypatch
):
    class UncertainBuyClient(ManagedClient):
        def __init__(self):
            super().__init__(with_position=False)
            self.account_calls = 0

        def get_order(self, **kwargs):
            raise RuntimeError("temporary order lookup failure")

        def get_accounts(self):
            self.account_calls += 1
            return super().get_accounts()

    client = UncertainBuyClient()
    engine = TradingEngine(
        client,
        storage=Storage(tmp_path / "unresolved-buy-start.db"),
    )
    engine.storage.create_order_intent(
        "uncertain-buy", "KRW-X", "BUY", {"entry_score": 80.0}
    )
    engine.storage.mark_order_submitted("uncertain-buy")
    monkeypatch.setattr(engine, "_run", lambda: None)

    engine.start()
    engine.wait()

    assert engine.storage.pending_orders()
    assert engine._state == EngineState.RUNNING
    assert engine._session_entry_locked is True
    assert engine._session_strategy_pnl_start is None
    assert client.account_calls == 0
    assert "미확정 주문 1건" in engine._session_entry_lock_reason
