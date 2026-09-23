from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, path: Path | None = None) -> None:
        base = Path.home() / ".junhyunbank"
        self.path = path or base / "junhyunbank.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS order_intents (
                identifier TEXT PRIMARY KEY, market TEXT NOT NULL, side TEXT NOT NULL,
                context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, exchange_order_id TEXT, error TEXT
            )""")
            self._ensure_column(conn, "order_intents", "submitted_at", "TEXT")
            self._ensure_column(conn, "order_intents", "accepted_at", "TEXT")
            # Null identifies legacy intents whose pre-POST state cannot be
            # proven. New-schema intents can safely distinguish a crash before
            # submission from an ambiguous request that may have reached Upbit.
            self._ensure_column(conn, "order_intents", "intent_version", "INTEGER")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    market TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL,
                    amount_krw REAL,
                    price REAL,
                    reason TEXT,
                    exchange_order_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS managed_positions (
                    market TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            additions = {
                "entry_price": "REAL",
                "entry_amount_krw": "REAL",
                "entry_fee_rate": "REAL",
                "initial_risk_pct": "REAL",
                "entry_score": "REAL",
                "signal_kind": "TEXT",
                "peak_price": "REAL",
                "expected_horizon_seconds": "REAL",
                "managed_quantity": "REAL",
                "realized_pnl_krw": "REAL DEFAULT 0",
                "trailing_stop_price": "REAL DEFAULT 0",
                "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
                "absence_count": "INTEGER NOT NULL DEFAULT 0",
                "last_balance_seen_at": "TEXT",
                "last_fill_at": "TEXT",
            }
            for name, definition in additions.items():
                self._ensure_column(conn, "managed_positions", name, definition)

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    signal_kind TEXT,
                    net_return_pct REAL NOT NULL,
                    normalized_return REAL NOT NULL
                )
                """
            )
            self._ensure_column(conn, "strategy_outcomes", "net_pnl_krw", "REAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_pnl_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    net_pnl_krw REAL NOT NULL,
                    reason TEXT NOT NULL,
                    exchange_order_id TEXT NOT NULL UNIQUE
                )
                """
            )

    def event(
        self,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events(created_at, level, event_type, message, payload) VALUES(?,?,?,?,?)",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    level,
                    event_type,
                    message,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                ),
            )

    def trade(
        self,
        *,
        mode: str,
        market: str,
        side: str,
        quantity: float | None,
        amount_krw: float | None,
        price: float | None,
        reason: str,
        exchange_order_id: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades(
                    created_at, mode, market, side, quantity, amount_krw,
                    price, reason, exchange_order_id
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    mode,
                    market,
                    side,
                    quantity,
                    amount_krw,
                    price,
                    reason,
                    exchange_order_id,
                ),
            )

    def mark_managed_position(
        self,
        market: str,
        *,
        entry_price: float | None = None,
        entry_amount_krw: float | None = None,
        entry_fee_rate: float | None = None,
        initial_risk_pct: float | None = None,
        entry_score: float | None = None,
        signal_kind: str | None = None,
        peak_price: float | None = None,
        expected_horizon_seconds: float | None = None,
        managed_quantity: float | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO managed_positions(
                    market, created_at, entry_price, entry_amount_krw,
                    entry_fee_rate, initial_risk_pct, entry_score, signal_kind,
                    peak_price, expected_horizon_seconds, managed_quantity
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(market) DO UPDATE SET
                    entry_price=COALESCE(excluded.entry_price, managed_positions.entry_price),
                    entry_amount_krw=COALESCE(excluded.entry_amount_krw, managed_positions.entry_amount_krw),
                    entry_fee_rate=COALESCE(excluded.entry_fee_rate, managed_positions.entry_fee_rate),
                    initial_risk_pct=COALESCE(excluded.initial_risk_pct, managed_positions.initial_risk_pct),
                    entry_score=COALESCE(excluded.entry_score, managed_positions.entry_score),
                    signal_kind=COALESCE(excluded.signal_kind, managed_positions.signal_kind),
                    peak_price=MAX(COALESCE(managed_positions.peak_price, 0), COALESCE(excluded.peak_price, 0)),
                    expected_horizon_seconds=COALESCE(excluded.expected_horizon_seconds, managed_positions.expected_horizon_seconds),
                    managed_quantity=COALESCE(excluded.managed_quantity, managed_positions.managed_quantity)
                """,
                (
                    market,
                    now,
                    entry_price,
                    entry_amount_krw,
                    entry_fee_rate,
                    initial_risk_pct,
                    entry_score,
                    signal_kind,
                    peak_price,
                    expected_horizon_seconds,
                    managed_quantity,
                ),
            )

    def get_managed_state(self, market: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM managed_positions WHERE market = ?",
                (market,),
            ).fetchone()
        return dict(row) if row else None

    def update_managed_quantity(self, market: str, quantity: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE managed_positions SET managed_quantity = ? WHERE market = ?",
                (max(0.0, quantity), market),
            )

    def update_managed_peak(self, market: str, price: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE managed_positions
                SET peak_price = MAX(COALESCE(peak_price, 0), ?)
                WHERE market = ?
                """,
                (max(0.0, price), market),
            )

    def update_managed_trailing_stop(self, market: str, price: float) -> None:
        """Persist the highest trailing stop so it cannot loosen after restart."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE managed_positions
                SET trailing_stop_price = MAX(
                    COALESCE(trailing_stop_price, 0), ?
                )
                WHERE market = ?
                """,
                (max(0.0, price), market),
            )

    def mark_managed_dust(self, market: str) -> None:
        """Keep cost basis while removing an untradeable remainder from drain."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE managed_positions SET status = 'DUST'
                WHERE market = ? AND COALESCE(status, 'ACTIVE') != 'QUARANTINED'
                """,
                (market,),
            )

    def mark_managed_tradeable(self, market: str) -> None:
        """Reactivate a previously dusty lot once its full value is orderable."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE managed_positions SET status = 'ACTIVE'
                WHERE market = ? AND COALESCE(status, 'ACTIVE') = 'DUST'
                """,
                (market,),
            )

    def quarantine_managed_position(self, market: str) -> None:
        """Irreversibly quarantine a lot whose account ownership is ambiguous."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE managed_positions SET status = 'QUARANTINED' WHERE market = ?",
                (market,),
            )

    def mark_managed_seen(self, market: str) -> str:
        """Record a valid account snapshot without reviving quarantined state."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM managed_positions WHERE market = ?", (market,)
            ).fetchone()
            if row is None:
                return ""
            status = str(row["status"] or "ACTIVE").upper()
            if status != "QUARANTINED":
                conn.execute(
                    """
                    UPDATE managed_positions
                    SET absence_count = 0, last_balance_seen_at = ?, status =
                        CASE WHEN status = 'DUST' THEN status ELSE 'ACTIVE' END
                    WHERE market = ?
                    """,
                    (now, market),
                )
            return status

    def record_managed_absence(
        self, market: str, *, quarantine_after: int = 3
    ) -> dict[str, Any] | None:
        """Count independent missing-balance snapshots and quarantine safely.

        A transient or eventually-consistent ``/accounts`` response must never
        delete the only durable record proving which quantity the program owns.
        Once repeated absence becomes credible, the position is quarantined so
        a later manual purchase cannot be mistaken for the original quantity.
        """
        threshold = max(2, int(quarantine_after))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM managed_positions WHERE market = ?", (market,)
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"] or "ACTIVE").upper()
            count = int(row["absence_count"] or 0)
            if status != "QUARANTINED":
                count += 1
                status = "QUARANTINED" if count >= threshold else status
                conn.execute(
                    """
                    UPDATE managed_positions
                    SET absence_count = ?, status = ?
                    WHERE market = ?
                    """,
                    (count, status, market),
                )
            return {**dict(row), "absence_count": count, "status": status}

    def drain_blocking_markets(self) -> set[str]:
        """Positions that can still be liquidated automatically while draining."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT market FROM managed_positions
                WHERE COALESCE(status, 'ACTIVE') NOT IN ('DUST', 'QUARANTINED')
                """
            ).fetchall()
        return {str(row["market"]) for row in rows}

    def unmark_managed_position(self, market: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM managed_positions WHERE market = ?", (market,))

    def managed_markets(self) -> set[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT market FROM managed_positions").fetchall()
        return {str(row["market"]) for row in rows}

    def record_outcome(
        self,
        *,
        market: str,
        signal_kind: str | None,
        net_return_pct: float,
        normalized_return: float,
        net_pnl_krw: float | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_outcomes(
                    created_at, market, signal_kind, net_return_pct,
                    normalized_return, net_pnl_krw
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    market,
                    signal_kind,
                    net_return_pct,
                    normalized_return,
                    net_pnl_krw,
                ),
            )

    def strategy_outcomes(self, limit: int = 50) -> list[float]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT normalized_return
                FROM strategy_outcomes
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [float(row["normalized_return"]) for row in reversed(rows)]

    def strategy_realized_pnl_total(self) -> float:
        """Cumulative audited KRW PnL for fully closed managed positions."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT net_pnl_krw FROM strategy_outcomes
                WHERE net_pnl_krw IS NOT NULL
                UNION ALL
                SELECT net_pnl_krw FROM strategy_pnl_adjustments
                """
            ).fetchall()
        total = 0.0
        for row in rows:
            try:
                value = float(row["net_pnl_krw"])
            except (TypeError, ValueError):
                return float("nan")
            # A corrupted ledger must close the entry gate. Coercing a
            # non-finite cumulative loss to zero could silently erase it for
            # session-risk purposes; the caller deliberately treats NaN as
            # unavailable.
            if not math.isfinite(value):
                return float("nan")
            total += value
            if not math.isfinite(total):
                return float("nan")
        return total

    def create_order_intent(self, identifier: str, market: str, side: str, context: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO order_intents(
                       identifier,market,side,context,created_at,intent_version
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    identifier,
                    market,
                    side,
                    json.dumps(context),
                    datetime.now().isoformat(timespec="seconds"),
                    1,
                ),
            )

    def mark_order_submitted(self, identifier: str) -> None:
        now = datetime.now().isoformat(timespec="milliseconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE order_intents SET submitted_at = ?
                WHERE identifier = ? AND status = 'pending'
                """,
                (now, identifier),
            )

    def mark_order_accepted(self, identifier: str, exchange_order_id: str) -> None:
        """Durably save the exchange UUID immediately after POST returns."""
        now = datetime.now().isoformat(timespec="milliseconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE order_intents
                SET exchange_order_id = ?, accepted_at = ?
                WHERE identifier = ? AND status = 'pending'
                """,
                (exchange_order_id or None, now, identifier),
            )

    def pending_orders(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM order_intents WHERE status='pending' ORDER BY created_at").fetchall()
        return [{**dict(row), 'context': json.loads(row['context'])} for row in rows]

    def activity_snapshot(self) -> dict:
        """Read confirmed fills and unresolved intents; never infer fills from signals."""
        today = datetime.now().date().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute('BEGIN')
            rows = conn.execute("SELECT id,created_at,market,side,quantity,amount_krw,price,reason FROM trades WHERE mode='LIVE' AND quantity>0 AND price>0 AND amount_krw>0 ORDER BY id DESC LIMIT 100").fetchall()
            counts = conn.execute("SELECT side,COUNT(*) AS count FROM trades WHERE mode='LIVE' AND quantity>0 AND price>0 AND amount_krw>0 AND created_at>=? GROUP BY side", (today,)).fetchall()
            pending = conn.execute("SELECT market,side,created_at FROM order_intents WHERE status='pending' ORDER BY created_at").fetchall()
        return dict(trades=[dict(r) for r in rows], counts={r['side']:r['count'] for r in counts},
                    pending=[dict(r) for r in pending], date=today)

    def reject_order_intent(self, identifier: str, error: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE order_intents SET status='rejected',error=? WHERE identifier=? AND status='pending'", (error, identifier))

    def reject_prepared_order_intent(self, identifier: str, error: str) -> bool:
        """Reject only a provably never-submitted new-schema intent.

        Legacy pending rows have no ``intent_version`` and may represent an
        ambiguous POST from an older release, so they deliberately remain
        pending for identifier reconciliation instead of being guessed away.
        """
        with self._lock, self._connect() as conn:
            result = conn.execute(
                """UPDATE order_intents
                   SET status='rejected', error=?
                   WHERE identifier=? AND status='pending'
                     AND intent_version=1
                     AND submitted_at IS NULL
                     AND accepted_at IS NULL
                     AND exchange_order_id IS NULL""",
                (error, identifier),
            )
        return result.rowcount == 1

    def complete_order_intent(self, identifier: str, detail: dict) -> bool:
        """Commit a terminal fill, position and outcome atomically, exactly once.

        Nonterminal/ambiguous responses remain pending across process restarts.
        The exchange account's unrelated balance is never used to infer a fill.
        """
        if (
            not isinstance(detail, dict)
            or detail.get("state") not in {"done", "cancel"}
            or not detail.get("uuid")
            or "executed_volume" not in detail
        ):
            return False

        try:
            executed = float(detail.get("executed_volume") or 0)
            fee = float(detail.get("paid_fee") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(value) and value >= 0 for value in (executed, fee)):
            return False

        raw_trades = detail.get("trades") or []
        if not isinstance(raw_trades, list):
            return False
        trade_volumes: list[float] = []
        trade_funds: list[float] = []
        try:
            for trade in raw_trades:
                if not isinstance(trade, dict):
                    return False
                trade_volume = float(trade.get("volume") or 0)
                if not math.isfinite(trade_volume) or trade_volume < 0:
                    return False

                raw_price = trade.get("price")
                trade_price = float(raw_price or 0)
                if not math.isfinite(trade_price) or trade_price < 0:
                    return False

                raw_funds = trade.get("funds")
                trade_value = (
                    float(raw_funds)
                    if raw_funds is not None
                    else trade_price * trade_volume
                )
                if not math.isfinite(trade_value) or trade_value < 0:
                    return False
                trade_volumes.append(trade_volume)
                trade_funds.append(trade_value)
            volume = math.fsum(trade_volumes)
            funds = math.fsum(trade_funds)
        except (TypeError, ValueError, OverflowError):
            return False

        if not all(math.isfinite(value) and value >= 0 for value in (volume, funds)):
            return False
        if not math.isclose(volume, executed, rel_tol=1e-7, abs_tol=1e-12):
            return False
        if executed > 0 and (funds <= 0 or fee >= funds):
            return False
        if executed == 0 and (funds != 0 or fee != 0):
            return False

        exchange_order_id = str(detail["uuid"])
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM order_intents WHERE identifier=? AND status='pending'",
                (identifier,),
            ).fetchone()
            if row is None:
                return False
            try:
                context = json.loads(row["context"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            if not isinstance(context, dict):
                return False

            market, side = str(row["market"]), str(row["side"])
            if side not in {"BUY", "SELL"}:
                return False
            if detail.get("identifier") is not None and detail["identifier"] != identifier:
                return False
            expected_side = "bid" if side == "BUY" else "ask"
            if (
                detail.get("market", market) != market
                or detail.get("side", expected_side) != expected_side
            ):
                return False
            accepted_order_id = row["exchange_order_id"]
            if accepted_order_id and str(accepted_order_id) != exchange_order_id:
                return False

            if executed > 0:
                price = funds / executed
                if not math.isfinite(price) or price <= 0:
                    return False
                now = datetime.now().isoformat(timespec="seconds")
                if side == "BUY":
                    if conn.execute(
                        "SELECT 1 FROM managed_positions WHERE market=?", (market,)
                    ).fetchone():
                        return False
                    try:
                        initial_risk = float(context.get("initial_risk_pct") or 0)
                        entry_score = float(context.get("entry_score") or 0)
                        horizon = float(
                            context.get("expected_horizon_seconds") or 300
                        )
                    except (TypeError, ValueError, OverflowError):
                        return False
                    if (
                        not math.isfinite(initial_risk)
                        or initial_risk < 0
                        or not math.isfinite(entry_score)
                        or not math.isfinite(horizon)
                        or horizon <= 0
                    ):
                        return False
                    entry_fee_rate = fee / funds
                    if not math.isfinite(entry_fee_rate) or not 0 <= entry_fee_rate < 1:
                        return False
                    conn.execute(
                        """INSERT INTO managed_positions(
                               market,created_at,entry_price,entry_amount_krw,
                               entry_fee_rate,initial_risk_pct,entry_score,
                               signal_kind,peak_price,expected_horizon_seconds,
                               managed_quantity,trailing_stop_price,status,
                               absence_count,last_balance_seen_at,last_fill_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            market,
                            now,
                            price,
                            funds,
                            entry_fee_rate,
                            initial_risk,
                            entry_score,
                            str(context.get("signal_kind") or "NONE"),
                            price,
                            horizon,
                            executed,
                            0.0,
                            "ACTIVE",
                            0,
                            now,
                            now,
                        ),
                    )
                else:
                    state = conn.execute(
                        "SELECT * FROM managed_positions WHERE market=?", (market,)
                    ).fetchone()
                    if state is None or state["managed_quantity"] is None:
                        return False
                    try:
                        quantity = float(state["managed_quantity"])
                        entry = float(state["entry_price"])
                        entry_fee_rate = float(state["entry_fee_rate"])
                        prior_realized = float(state["realized_pnl_krw"] or 0)
                        min_ask_krw = float(context.get("min_ask_krw", 0))
                    except (TypeError, ValueError, OverflowError):
                        return False
                    if (
                        not math.isfinite(quantity)
                        or quantity <= 0
                        or not math.isfinite(entry)
                        or entry <= 0
                        or not math.isfinite(entry_fee_rate)
                        or not 0 <= entry_fee_rate < 1
                        or not math.isfinite(prior_realized)
                        or not math.isfinite(min_ask_krw)
                        or min_ask_krw < 0
                    ):
                        return False
                    if executed > quantity + 1e-12:
                        return False
                    remaining = max(0.0, quantity - executed)
                    realized = (
                        prior_realized
                        + funds
                        - fee
                        - executed * entry * (1 + entry_fee_rate)
                    )
                    if not math.isfinite(remaining) or not math.isfinite(realized):
                        return False

                    if remaining <= 1e-12:
                        try:
                            initial_amount = float(state["entry_amount_krw"])
                        except (TypeError, ValueError, OverflowError):
                            initial_amount = float("nan")
                        try:
                            risk = float(state["initial_risk_pct"])
                        except (TypeError, ValueError, OverflowError):
                            risk = float("nan")
                        valid_basis = (
                            math.isfinite(initial_amount)
                            and initial_amount > 0
                            and math.isfinite(risk)
                            and risk > 0
                        )
                        if valid_basis:
                            net = realized / initial_amount
                            normalized = net / risk
                            if not math.isfinite(net) or not math.isfinite(normalized):
                                return False
                            conn.execute(
                                """INSERT INTO strategy_outcomes(
                                       created_at,market,signal_kind,net_return_pct,
                                       normalized_return,net_pnl_krw
                                   ) VALUES(?,?,?,?,?,?)""",
                                (
                                    now,
                                    market,
                                    state["signal_kind"],
                                    net,
                                    normalized,
                                    realized,
                                ),
                            )
                        else:
                            invalid_fields = []
                            if not math.isfinite(initial_amount) or initial_amount <= 0:
                                invalid_fields.append("entry_amount_krw")
                            if not math.isfinite(risk) or risk <= 0:
                                invalid_fields.append("initial_risk_pct")
                            inserted = conn.execute(
                                """INSERT OR IGNORE INTO strategy_pnl_adjustments(
                                       created_at,market,net_pnl_krw,reason,
                                       exchange_order_id
                                   ) VALUES(?,?,?,?,?)""",
                                (
                                    now,
                                    market,
                                    realized,
                                    "invalid outcome basis: " + ",".join(invalid_fields),
                                    exchange_order_id,
                                ),
                            )
                            if inserted.rowcount != 1:
                                return False
                        conn.execute(
                            "DELETE FROM managed_positions WHERE market=?", (market,)
                        )
                    else:
                        conn.execute(
                            """UPDATE managed_positions
                               SET managed_quantity=?, realized_pnl_krw=?
                               WHERE market=?""",
                            (remaining, realized, market),
                        )
                    if remaining > 1e-12 and remaining * price < min_ask_krw:
                        # Keep the cost basis and managed quantity. Treating an
                        # unsold remainder as a completed trade biases Strategy
                        # Health and can later expose a user's same-asset holding.
                        conn.execute(
                            "UPDATE managed_positions SET status='DUST' WHERE market=?",
                            (market,),
                        )
                conn.execute(
                    """INSERT INTO trades(
                           created_at,mode,market,side,quantity,amount_krw,
                           price,reason,exchange_order_id
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        now,
                        "LIVE",
                        market,
                        side,
                        executed,
                        funds,
                        price,
                        str(context.get("reason") or ""),
                        exchange_order_id,
                    ),
                )
            conn.execute(
                """UPDATE order_intents
                   SET status='complete',exchange_order_id=?
                   WHERE identifier=?""",
                (exchange_order_id, identifier),
            )
        return True
