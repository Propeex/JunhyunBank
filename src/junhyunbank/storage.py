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
        return sqlite3.connect(self.path, timeout=10)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT
                )
                '''
            )
            conn.execute(
                '''
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
                '''
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
                '''
                INSERT INTO trades(
                    created_at, mode, market, side, quantity, amount_krw,
                    price, reason, exchange_order_id
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ''',
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
