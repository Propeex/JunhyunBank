import time
from pathlib import Path

from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.storage import Storage


def terminal_buy(identifier: str) -> dict:
    return {
        "uuid": "exchange-private-1",
        "identifier": identifier,
        "market": "KRW-X",
        "side": "bid",
        "state": "cancel",
        "executed_volume": "2",
        "paid_fee": "10",
        "trades": [
            {"volume": "2", "price": "10000", "funds": "20000"},
        ],
    }


class Client:
    access_key = "access"
    secret_key = "secret"

    def __init__(self) -> None:
        self.calls = []
        self.details = {}
        self.error = None

    def get_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.details[kwargs["identifier"]]


def engine(tmp_path: Path) -> TradingEngine:
    return TradingEngine(Client(), storage=Storage(tmp_path / "private.db"))


def create_buy_intent(e: TradingEngine, identifier: str) -> None:
    e.storage.create_order_intent(
        identifier,
        "KRW-X",
        "BUY",
        {
            "reason": "test",
            "initial_risk_pct": 0.01,
            "entry_score": 90.0,
            "signal_kind": "IGNITION",
            "expected_horizon_seconds": 60.0,
        },
    )


def test_myorder_event_accelerates_canonical_rest_reconciliation(tmp_path):
    e = engine(tmp_path)
    identifier = "junhyunbank-test-private-order"
    create_buy_intent(e, identifier)
    e.client.details[identifier] = terminal_buy(identifier)
    e._order_reconcile_retry_at[identifier] = time.monotonic() + 60.0

    e._on_private_order(
        {
            "type": "myOrder",
            "code": "KRW-X",
            "identifier": identifier,
            "state": "done",
            "executed_volume": 2,
        }
    )

    # WebSocket notification alone does not mutate accounting state.
    assert e.storage.pending_orders()
    assert not e.storage.managed_markets()

    e._reconcile_orders()

    assert e.client.calls == [{"identifier": identifier}]
    assert e.storage.pending_orders() == []
    assert e.storage.get_managed_state("KRW-X")["managed_quantity"] == 2


def test_manual_order_event_never_wakes_or_mutates_junhyunbank_intent(tmp_path):
    e = engine(tmp_path)
    identifier = "junhyunbank-pending"
    create_buy_intent(e, identifier)
    e.client.details[identifier] = terminal_buy(identifier)
    e._order_reconcile_retry_at[identifier] = time.monotonic() + 60.0

    e._on_private_order(
        {
            "type": "myOrder",
            "code": "KRW-X",
            "identifier": "manual-order-123",
            "state": "done",
        }
    )
    e._reconcile_orders()

    assert e.client.calls == []
    assert len(e.storage.pending_orders()) == 1
    assert not e.storage.managed_markets()


def test_myasset_is_signal_only_and_never_replaces_managed_quantity(tmp_path):
    e = engine(tmp_path)
    e.storage.mark_managed_position("KRW-X", managed_quantity=2, entry_price=10000)

    e._on_private_asset(
        {
            "type": "myAsset",
            "assets": [{"currency": "X", "balance": 99, "locked": 1}],
        }
    )

    assert e.storage.get_managed_state("KRW-X")["managed_quantity"] == 2
    events = e.drain_events()
    assert any(row.get("type") == "private_asset" for row in events)


def test_repeated_rest_failure_backs_off_pending_lookup(tmp_path):
    e = engine(tmp_path)
    identifier = "junhyunbank-failing"
    create_buy_intent(e, identifier)
    e.client.error = RuntimeError("temporary lookup failure")

    e._reconcile_orders()
    e._reconcile_orders()

    assert e.client.calls == [{"identifier": identifier}]
    assert e._order_reconcile_retry_at[identifier] > time.monotonic()
    assert len(e.storage.pending_orders()) == 1


def test_private_event_overrides_existing_failure_backoff(tmp_path):
    e = engine(tmp_path)
    identifier = "junhyunbank-recover-after-event"
    create_buy_intent(e, identifier)
    e.client.error = RuntimeError("temporary")
    e._reconcile_orders()
    assert len(e.client.calls) == 1

    e.client.error = None
    e.client.details[identifier] = terminal_buy(identifier)
    e._on_private_order(
        {
            "type": "myOrder",
            "identifier": identifier,
            "code": "KRW-X",
            "state": "trade",
        }
    )
    e._reconcile_orders()

    assert len(e.client.calls) == 2
    assert e.storage.pending_orders() == []
