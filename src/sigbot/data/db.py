"""SQLite storage. Keep everything: the history is the dataset you'll calibrate on later."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ..api.models import Market, OrderBook

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    categories TEXT,
    settlement_date TEXT,
    settled_with TEXT,
    yes_exchange_id TEXT,
    is_binary INTEGER,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS price_snapshots (
    ts TEXT, exchange_id TEXT, market_id TEXT,
    latest REAL, best_bid REAL, best_ask REAL, spread REAL
);
CREATE INDEX IF NOT EXISTS ix_price_ex_ts ON price_snapshots(exchange_id, ts);
CREATE TABLE IF NOT EXISTS book_snapshots (
    ts TEXT, exchange_id TEXT, market_id TEXT, as_of_seq INTEGER,
    best_bid REAL, best_ask REAL, bids TEXT, asks TEXT
);
CREATE INDEX IF NOT EXISTS ix_book_ex_ts ON book_snapshots(exchange_id, ts);
CREATE TABLE IF NOT EXISTS signals (
    ts TEXT, mode TEXT, market_id TEXT, exchange_id TEXT,
    side TEXT, action TEXT, price REAL, quantity INTEGER,
    p_model REAL, p_market REAL, uncertainty REAL, edge REAL,
    status TEXT, order_id TEXT, response TEXT
);
CREATE TABLE IF NOT EXISTS arb_repairs (
    race TEXT PRIMARY KEY, data TEXT, created TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS bot_status (
    id INTEGER PRIMARY KEY CHECK (id = 1), ts TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
    market_id TEXT PRIMARY KEY, settled_with TEXT, recorded_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def upsert_markets(self, markets: Iterable[Market]) -> None:
        ts = now_iso()
        rows = [
            (m.id, m.title, m.status, json.dumps(m.categories), m.settlement_date, m.settled_with,
             m.yes_exchange_id, int(m.is_binary), ts)
            for m in markets
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        for m in markets:
            if m.status == "settled" and m.settled_with is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO outcomes VALUES (?,?,?)", (m.id, m.settled_with, ts)
                )
        self.conn.commit()

    def insert_prices(self, prices: Iterable[Dict[str, Any]]) -> None:
        ts = now_iso()
        self.conn.executemany(
            "INSERT INTO price_snapshots VALUES (?,?,?,?,?,?,?)",
            [(ts, p["exchangeId"], p.get("marketId"), p.get("latestPrice"), p.get("bestBid"),
              p.get("bestAsk"), p.get("spread")) for p in prices],
        )
        self.conn.commit()

    def insert_book(self, book: OrderBook) -> None:
        self.conn.execute(
            "INSERT INTO book_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (now_iso(), book.exchange_id, book.market_id, book.as_of_seq, book.best_bid, book.best_ask,
             json.dumps([[l.price, l.quantity] for l in book.bids]),
             json.dumps([[l.price, l.quantity] for l in book.asks])),
        )
        self.conn.commit()

    def log_signal(self, **row: Any) -> None:
        cols = ["ts", "mode", "market_id", "exchange_id", "side", "action", "price", "quantity",
                "p_model", "p_market", "uncertainty", "edge", "status", "order_id", "response"]
        row.setdefault("ts", now_iso())
        if isinstance(row.get("response"), (dict, list)):
            row["response"] = json.dumps(row["response"])
        self.conn.execute(
            f"INSERT INTO signals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row.get(c) for c in cols],
        )
        self.conn.commit()

    def set_status(self, status: Dict[str, Any]) -> None:
        """The arb bot's latest heartbeat, for the dashboard. One row, overwritten each cycle."""
        self.conn.execute("INSERT OR REPLACE INTO bot_status VALUES (1, ?, ?)", (now_iso(), json.dumps(status)))
        self.conn.commit()

    def get_repairs(self) -> Dict[str, Dict[str, Any]]:
        """Races the arb bot holds unevenly and is still completing: race → repair."""
        return {r["race"]: {**json.loads(r["data"]), "created": r["created"], "updated": r["updated"]}
                for r in self.conn.execute("SELECT * FROM arb_repairs")}

    def save_repair(self, race: str, data: Dict[str, Any]) -> None:
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO arb_repairs VALUES (?,?,?,?) ON CONFLICT(race) DO UPDATE SET data=excluded.data, updated=?",
            (race, json.dumps(data), ts, ts, ts))
        self.conn.commit()

    def delete_repair(self, race: str) -> None:
        self.conn.execute("DELETE FROM arb_repairs WHERE race=?", (race,))
        self.conn.commit()

    def get_status(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT ts, status FROM bot_status WHERE id = 1").fetchone()
        return {"ts": r["ts"], **json.loads(r["status"])} if r else None

    def query(self, sql: str, params: tuple = ()) -> list:
        return self.conn.execute(sql, params).fetchall()

    def outcome(self, market_id: str) -> Optional[str]:
        r = self.conn.execute("SELECT settled_with FROM outcomes WHERE market_id=?", (market_id,)).fetchone()
        return r[0] if r else None
