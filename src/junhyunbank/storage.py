from __future__ import annotations

import json
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
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_outcomes(
                    created_at, market, signal_kind, net_return_pct, normalized_return
                ) VALUES(?,?,?,?,?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    market,
                    signal_kind,
                    net_return_pct,
                    normalized_return,
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

    def create_order_intent(self, identifier: str, market: str, side: str, context: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO order_intents(identifier,market,side,context,created_at) VALUES(?,?,?,?,?)",
                         (identifier, market, side, json.dumps(context), datetime.now().isoformat(timespec='seconds')))

    def pending_orders(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM order_intents WHERE status='pending' ORDER BY created_at").fetchall()
        return [{**dict(row), 'context': json.loads(row['context'])} for row in rows]

    def reject_order_intent(self, identifier: str, error: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE order_intents SET status='rejected',error=? WHERE identifier=? AND status='pending'", (error, identifier))

    def complete_order_intent(self, identifier: str, detail: dict) -> bool:
        """Commit a terminal fill, position and outcome atomically, exactly once.

        Nonterminal/ambiguous responses remain pending across process restarts.
        The exchange account's unrelated balance is never used to infer a fill.
        """
        if detail.get('state') not in {'done', 'cancel'} or not detail.get('uuid') or 'executed_volume' not in detail:
            return False
        import math
        executed = float(detail.get('executed_volume') or 0)
        trades = detail.get('trades') or []
        volume = sum(float(t.get('volume') or 0) for t in trades)
        funds = sum(float(t['funds']) if t.get('funds') is not None else float(t.get('price') or 0) * float(t.get('volume') or 0) for t in trades)
        fee = float(detail.get('paid_fee') or 0)
        if not all(math.isfinite(x) and x >= 0 for x in (executed, volume, funds, fee)):
            return False
        if executed > 0 and (funds <= 0 or not math.isclose(volume, executed, rel_tol=1e-7, abs_tol=1e-12)):
            return False
        with self._lock, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM order_intents WHERE identifier=? AND status='pending'", (identifier,)).fetchone()
            if row is None:
                return False
            market, side, context = row['market'], row['side'], json.loads(row['context'])
            if detail.get('identifier') is not None and detail['identifier'] != identifier:
                return False
            if detail.get('market', market) != market or detail.get('side', 'bid' if side == 'BUY' else 'ask') != ('bid' if side == 'BUY' else 'ask'):
                return False
            if executed > 0:
                price = funds / executed
                now = datetime.now().isoformat(timespec='seconds')
                if side == 'BUY':
                    if conn.execute('SELECT 1 FROM managed_positions WHERE market=?', (market,)).fetchone():
                        return False
                    conn.execute('''INSERT INTO managed_positions(market,created_at,entry_price,entry_amount_krw,entry_fee_rate,initial_risk_pct,entry_score,signal_kind,peak_price,expected_horizon_seconds,managed_quantity)
                                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                                 (market,row['created_at'],price,funds,fee/funds,context['initial_risk_pct'],context['entry_score'],context['signal_kind'],price,context['expected_horizon_seconds'],executed))
                else:
                    state = conn.execute('SELECT * FROM managed_positions WHERE market=?', (market,)).fetchone()
                    if state is None or state['managed_quantity'] is None:
                        return False
                    quantity = float(state['managed_quantity'])
                    if executed > quantity + 1e-12:
                        return False
                    remaining = max(0, quantity-executed)
                    conn.execute('UPDATE managed_positions SET managed_quantity=? WHERE market=?', (remaining, market))
                    entry = float(state['entry_price'] or 0)
                    initial_amount = float(state['entry_amount_krw'] or 0)
                    risk = max(float(state['initial_risk_pct'] or 0), 1e-6)
                    realized = float(state['realized_pnl_krw'] or 0) + funds-fee-executed*entry*(1+float(state['entry_fee_rate'] or 0))
                    conn.execute('UPDATE managed_positions SET realized_pnl_krw=? WHERE market=?', (realized,market))
                    if remaining <= 1e-12 or remaining*price < float(context.get('min_ask_krw', 0)):
                        if entry > 0 and initial_amount > 0:
                            net = realized / initial_amount
                            conn.execute('INSERT INTO strategy_outcomes(created_at,market,signal_kind,net_return_pct,normalized_return) VALUES(?,?,?,?,?)',
                                         (now,market,state['signal_kind'],net,net/risk))
                        conn.execute('DELETE FROM managed_positions WHERE market=?', (market,))
                conn.execute('''INSERT INTO trades(created_at,mode,market,side,quantity,amount_krw,price,reason,exchange_order_id) VALUES(?,?,?,?,?,?,?,?,?)''',
                             (now,'LIVE',market,side,executed,funds,price,context['reason'],detail.get('uuid')))
            conn.execute("UPDATE order_intents SET status='complete',exchange_order_id=? WHERE identifier=?", (detail.get('uuid'),identifier))
        return True
