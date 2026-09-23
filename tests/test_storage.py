import math

import pytest

from junhyunbank.storage import Storage


def test_managed_positions_are_persisted_with_v2_state(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.mark_managed_position("KRW-BTC", entry_price=100.0, initial_risk_pct=0.02, managed_quantity=1.5, signal_kind="IGNITION")
    assert storage.managed_markets() == {"KRW-BTC"}
    state = storage.get_managed_state("KRW-BTC")
    assert state["entry_price"] == 100.0
    assert state["managed_quantity"] == 1.5
    storage.unmark_managed_position("KRW-BTC")
    assert storage.managed_markets() == set()


def test_strategy_outcomes_are_persisted(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.record_outcome(market="KRW-BTC", signal_kind="IGNITION", net_return_pct=0.01, normalized_return=0.5)
    assert storage.strategy_outcomes(10) == [0.5]


def test_strategy_realized_pnl_ledger_is_separate_from_normalized_health(tmp_path):
    storage = Storage(tmp_path / "pnl.db")
    storage.record_outcome(
        market="KRW-X",
        signal_kind="IGNITION",
        net_return_pct=-0.02,
        normalized_return=-0.5,
        net_pnl_krw=-2_500.0,
    )

    assert storage.strategy_outcomes(10) == [-0.5]
    assert storage.strategy_realized_pnl_total() == -2_500.0


def test_nonfinite_realized_pnl_ledger_fails_closed(tmp_path):
    storage = Storage(tmp_path / "corrupt-pnl.db")
    with storage._connect() as conn:
        conn.execute(
            """
            INSERT INTO strategy_outcomes(
                created_at, market, signal_kind, net_return_pct,
                normalized_return, net_pnl_krw
            ) VALUES(?,?,?,?,?,?)
            """,
            ("2026-01-01T00:00:00", "KRW-X", "IGNITION", 0.0, 0.0, float("inf")),
        )

    assert math.isnan(storage.strategy_realized_pnl_total())


def test_new_schema_prepared_intent_can_be_rejected_as_never_submitted(tmp_path):
    storage = Storage(tmp_path / "prepared-intent.db")
    storage.create_order_intent("prepared-1", "KRW-X", "BUY", {})

    assert storage.reject_prepared_order_intent(
        "prepared-1", "crash before POST"
    )
    assert storage.pending_orders() == []


def test_submitted_or_legacy_ambiguous_intent_is_never_guessed_away(tmp_path):
    storage = Storage(tmp_path / "ambiguous-intent.db")
    storage.create_order_intent("submitted-1", "KRW-X", "BUY", {})
    storage.mark_order_submitted("submitted-1")
    with storage._connect() as conn:
        conn.execute(
            """INSERT INTO order_intents(
                   identifier,market,side,context,created_at,intent_version
               ) VALUES(?,?,?,?,?,NULL)""",
            ("legacy-1", "KRW-Y", "BUY", "{}", "2026-01-01T00:00:00"),
        )

    assert not storage.reject_prepared_order_intent(
        "submitted-1", "must remain ambiguous"
    )
    assert not storage.reject_prepared_order_intent(
        "legacy-1", "must remain ambiguous"
    )
    assert {row["identifier"] for row in storage.pending_orders()} == {
        "submitted-1",
        "legacy-1",
    }


def _sell_fill(*, order_id="sell-1", quantity=10.0, price=90.0):
    funds = quantity * price
    return {
        "uuid": order_id,
        "market": "KRW-X",
        "side": "ask",
        "state": "done",
        "executed_volume": str(quantity),
        "paid_fee": "0",
        "trades": [
            {
                "volume": str(quantity),
                "price": str(price),
                "funds": str(funds),
            }
        ],
    }


def _corrupt_basis_sell(storage, *, identifier="sell-intent-1", order_id="sell-1"):
    storage.mark_managed_position(
        "KRW-X",
        entry_price=100.0,
        entry_amount_krw=0.0,
        entry_fee_rate=0.0,
        initial_risk_pct=0.1,
        managed_quantity=10.0,
        signal_kind="IGNITION",
    )
    storage.create_order_intent(
        identifier,
        "KRW-X",
        "SELL",
        {"reason": "test close", "min_ask_krw": 5_000},
    )
    return storage.complete_order_intent(
        identifier, _sell_fill(order_id=order_id)
    )


def test_full_sell_with_corrupt_principal_keeps_loss_in_adjustment_ledger(tmp_path):
    storage = Storage(tmp_path / "adjustment.db")

    assert _corrupt_basis_sell(storage)

    assert storage.managed_markets() == set()
    assert storage.strategy_outcomes() == []
    assert storage.strategy_realized_pnl_total() == pytest.approx(-100.0)
    with storage._connect() as conn:
        adjustment = conn.execute(
            "SELECT * FROM strategy_pnl_adjustments"
        ).fetchone()
    assert adjustment["market"] == "KRW-X"
    assert adjustment["net_pnl_krw"] == pytest.approx(-100.0)
    assert adjustment["exchange_order_id"] == "sell-1"
    assert "entry_amount_krw" in adjustment["reason"]


def test_adjustment_exchange_order_id_is_idempotent(tmp_path):
    storage = Storage(tmp_path / "adjustment-idempotency.db")
    assert _corrupt_basis_sell(storage)

    assert not _corrupt_basis_sell(
        storage,
        identifier="sell-intent-2",
        order_id="sell-1",
    )

    assert storage.strategy_realized_pnl_total() == pytest.approx(-100.0)
    assert storage.managed_markets() == {"KRW-X"}
    assert {row["identifier"] for row in storage.pending_orders()} == {
        "sell-intent-2"
    }
    with storage._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM strategy_pnl_adjustments"
        ).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("entry_price", float("inf")),
        ("entry_fee_rate", float("inf")),
        ("realized_pnl_krw", float("inf")),
    ],
)
def test_nonfinite_core_sell_accounting_remains_pending(
    tmp_path, column, value
):
    storage = Storage(tmp_path / f"nonfinite-{column}.db")
    storage.mark_managed_position(
        "KRW-X",
        entry_price=100.0,
        entry_amount_krw=1_000.0,
        entry_fee_rate=0.0,
        initial_risk_pct=0.1,
        managed_quantity=10.0,
    )
    with storage._connect() as conn:
        conn.execute(
            f"UPDATE managed_positions SET {column}=? WHERE market='KRW-X'",
            (value,),
        )
    storage.create_order_intent("sell-intent", "KRW-X", "SELL", {})

    assert not storage.complete_order_intent("sell-intent", _sell_fill())

    assert storage.managed_markets() == {"KRW-X"}
    assert [row["identifier"] for row in storage.pending_orders()] == [
        "sell-intent"
    ]
    assert storage.strategy_realized_pnl_total() == 0.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executed_volume", "nan"),
        ("paid_fee", "inf"),
    ],
)
def test_nonfinite_exchange_fill_remains_pending(tmp_path, field, value):
    storage = Storage(tmp_path / f"nonfinite-fill-{field}.db")
    storage.mark_managed_position(
        "KRW-X",
        entry_price=100.0,
        entry_amount_krw=1_000.0,
        entry_fee_rate=0.0,
        initial_risk_pct=0.1,
        managed_quantity=10.0,
    )
    storage.create_order_intent("sell-intent", "KRW-X", "SELL", {})
    detail = _sell_fill()
    detail[field] = value

    assert not storage.complete_order_intent("sell-intent", detail)

    assert storage.managed_markets() == {"KRW-X"}
    assert storage.pending_orders()
    assert storage.strategy_realized_pnl_total() == 0.0


def test_dust_can_rejoin_drain_once_tradeable(tmp_path):
    storage = Storage(tmp_path / "dust-recovery.db")
    storage.mark_managed_position("KRW-X", entry_price=100.0, managed_quantity=1.0)
    storage.mark_managed_dust("KRW-X")
    assert storage.drain_blocking_markets() == set()

    storage.mark_managed_tradeable("KRW-X")

    assert storage.get_managed_state("KRW-X")["status"] == "ACTIVE"
    assert storage.drain_blocking_markets() == {"KRW-X"}


def test_explicit_quantity_mismatch_quarantine_cannot_be_revived(tmp_path):
    storage = Storage(tmp_path / "quantity-mismatch.db")
    storage.mark_managed_position("KRW-X", entry_price=100.0, managed_quantity=2.0)

    storage.quarantine_managed_position("KRW-X")
    storage.mark_managed_tradeable("KRW-X")
    storage.mark_managed_seen("KRW-X")

    assert storage.get_managed_state("KRW-X")["status"] == "QUARANTINED"
    assert storage.drain_blocking_markets() == set()


def test_trailing_stop_only_ratchets_up_in_durable_state(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.mark_managed_position("KRW-BTC", managed_quantity=1.0)

    storage.update_managed_trailing_stop("KRW-BTC", 95.0)
    storage.update_managed_trailing_stop("KRW-BTC", 90.0)
    assert storage.get_managed_state("KRW-BTC")["trailing_stop_price"] == 95.0

    storage.update_managed_trailing_stop("KRW-BTC", 96.0)
    assert storage.get_managed_state("KRW-BTC")["trailing_stop_price"] == 96.0


def test_repeated_missing_balances_quarantine_without_forgetting_position(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.mark_managed_position("KRW-BTC", managed_quantity=1.0)

    first = storage.record_managed_absence("KRW-BTC", quarantine_after=3)
    second = storage.record_managed_absence("KRW-BTC", quarantine_after=3)
    third = storage.record_managed_absence("KRW-BTC", quarantine_after=3)

    assert (first["absence_count"], first["status"]) == (1, "ACTIVE")
    assert (second["absence_count"], second["status"]) == (2, "ACTIVE")
    assert (third["absence_count"], third["status"]) == (3, "QUARANTINED")
    assert storage.managed_markets() == {"KRW-BTC"}
    assert storage.drain_blocking_markets() == set()

    # A later account row is not enough to prove that the same owned lot returned.
    assert storage.mark_managed_seen("KRW-BTC") == "QUARANTINED"
    state = storage.get_managed_state("KRW-BTC")
    assert state["status"] == "QUARANTINED"
    assert state["absence_count"] == 3


def test_seen_balance_resets_transient_absence_before_quarantine(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.mark_managed_position("KRW-BTC", managed_quantity=1.0)
    storage.record_managed_absence("KRW-BTC", quarantine_after=3)

    assert storage.mark_managed_seen("KRW-BTC") == "ACTIVE"
    state = storage.get_managed_state("KRW-BTC")
    assert state["absence_count"] == 0
    assert state["last_balance_seen_at"]


def test_order_intent_records_submission_and_exchange_acceptance_separately(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.create_order_intent(
        "intent-1", "KRW-BTC", "BUY", {"reason": "test"}
    )

    pending = storage.pending_orders()[0]
    assert pending["submitted_at"] is None
    assert pending["accepted_at"] is None
    assert pending["exchange_order_id"] is None

    storage.mark_order_submitted("intent-1")
    submitted = storage.pending_orders()[0]
    assert submitted["submitted_at"]
    assert submitted["accepted_at"] is None

    storage.mark_order_accepted("intent-1", "exchange-1")
    accepted = storage.pending_orders()[0]
    assert accepted["status"] == "pending"
    assert accepted["exchange_order_id"] == "exchange-1"
    assert accepted["accepted_at"]
