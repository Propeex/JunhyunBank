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
        base.mkdir(parents=True, exist_ok=True)
        self.path = path or base / "junhyunbank.db"
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
