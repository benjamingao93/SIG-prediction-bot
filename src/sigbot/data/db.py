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
CREATE TABLE IF NOT EXISTS kalshi_quotes (
    ts TEXT, race TEXT, party TEXT, bid REAL, ask REAL, last REAL,
    open_interest REAL, volume_24h REAL, tickers TEXT
);
CREATE INDEX IF NOT EXISTS ix_kalshi_race_ts ON kalshi_quotes(race, ts);
CREATE TABLE IF NOT EXISTS edges_history (
    ts TEXT, race TEXT, view TEXT, price REAL, fair REAL, edge REAL, required REAL,
    agree INTEGER, sig_p_d REAL, kalshi_p_d REAL, rating_p_d REAL
);
CREATE INDEX IF NOT EXISTS ix_edges_history ON edges_history(race, view, ts);
CREATE TABLE IF NOT EXISTS dir_positions (
    mode TEXT, race TEXT, view TEXT, exchange_id TEXT, market_id TEXT, title TEXT,
    qty REAL, cost REAL, entry_price REAL, fair_entry REAL, halved INTEGER DEFAULT 0,
    realized REAL DEFAULT 0, opened TEXT, updated TEXT, PRIMARY KEY (mode, race)
);
CREATE TABLE IF NOT EXISTS dir_realized (
    mode TEXT PRIMARY KEY, realized REAL
);
CREATE TABLE IF NOT EXISTS edges_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1), ts TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS arb_quotes (
    race TEXT PRIMARY KEY, data TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS arb_repairs (
    race TEXT PRIMARY KEY, data TEXT, created TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS bot_alive (
    id INTEGER PRIMARY KEY CHECK (id = 1), ts TEXT, cycle_started REAL, cycle INTEGER, mode TEXT,
    starting INTEGER DEFAULT 0
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
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(bot_alive)")}
        if "starting" not in cols:  # databases created before the column existed
            self.conn.execute("ALTER TABLE bot_alive ADD COLUMN starting INTEGER DEFAULT 0")

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

    def add_to_repair(self, race: str, side: str, legs: Iterable[Dict[str, Any]], source: str) -> Dict[str, Any]:
        """Add shortfalls to a race's repair, creating it if needed. A leg that already has one
        gets the quantities summed and the caps averaged by quantity."""
        rep = self.get_repairs().get(race) or {"side": side, "legs": [], "source": source}
        rep.pop("created", None)
        rep.pop("updated", None)
        by_ex = {l["exchange_id"]: l for l in rep["legs"]}
        for new in legs:
            old = by_ex.get(new["exchange_id"])
            if old is None or old["short"] < 1:
                by_ex[new["exchange_id"]] = dict(new)
            else:
                total = old["short"] + new["short"]
                old["cap"] = round((old["cap"] * old["short"] + new["cap"] * new["short"]) / total, 3)
                old["short"] = total
        rep["legs"] = list(by_ex.values())
        self.save_repair(race, rep)
        return rep

    def insert_kalshi(self, quotes: Iterable[Any]) -> None:
        """Kalshi quotes (data/external/kalshi.KalshiQuote), one snapshot per fetch."""
        ts = now_iso()
        self.conn.executemany(
            "INSERT INTO kalshi_quotes VALUES (?,?,?,?,?,?,?,?,?)",
            [(ts, q.race, q.party, q.bid, q.ask, q.last, q.open_interest, q.volume_24h, q.tickers) for q in quotes])
        self.conn.commit()

    def latest_kalshi(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """race → party → the newest Kalshi quote row (with its ts)."""
        rows = self.conn.execute(
            "SELECT k.* FROM kalshi_quotes k JOIN (SELECT race, MAX(ts) ts FROM kalshi_quotes GROUP BY race) m "
            "ON k.race = m.race AND k.ts = m.ts").fetchall()
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["race"], {})[r["party"]] = dict(r)
        return out

    def insert_edges_history(self, rows: Iterable[Dict[str, Any]]) -> None:
        ts = now_iso()
        self.conn.executemany(
            "INSERT INTO edges_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, r["race"], r["view"], r["price"], r["fair"], r["edge"], r["required"], int(bool(r["agree"])),
              r.get("sig_p_d"), r.get("kalshi_p_d"), r.get("rating_p_d")) for r in rows])
        self.conn.commit()

    # ---- directional ledger: positions the directional trader holds, kept apart from baskets ----

    def dir_positions(self, mode: str) -> Dict[str, Dict[str, Any]]:
        return {r["race"]: dict(r) for r in self.conn.execute("SELECT * FROM dir_positions WHERE mode = ?", (mode,))}

    def save_dir_position(self, p: Dict[str, Any]) -> None:
        cols = ["mode", "race", "view", "exchange_id", "market_id", "title", "qty", "cost", "entry_price",
                "fair_entry", "halved", "realized", "opened", "updated"]
        p = {**p, "updated": now_iso(), "opened": p.get("opened") or now_iso()}
        self.conn.execute(f"INSERT OR REPLACE INTO dir_positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [p.get(c) for c in cols])
        self.conn.commit()

    def close_dir_position(self, mode: str, race: str) -> None:
        self.conn.execute("DELETE FROM dir_positions WHERE mode = ? AND race = ?", (mode, race))
        self.conn.commit()

    def add_dir_realized(self, mode: str, amount: float) -> float:
        cur = self.dir_realized(mode) + amount
        self.conn.execute("INSERT OR REPLACE INTO dir_realized VALUES (?, ?)", (mode, cur))
        self.conn.commit()
        return cur

    def dir_realized(self, mode: str) -> float:
        r = self.conn.execute("SELECT realized FROM dir_realized WHERE mode = ?", (mode,)).fetchone()
        return float(r["realized"]) if r else 0.0

    def set_edges(self, data: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO edges_snapshot VALUES (1, ?, ?)", (now_iso(), json.dumps(data)))
        self.conn.commit()

    def get_edges(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT ts, data FROM edges_snapshot WHERE id = 1").fetchone()
        return {"ts": r["ts"], "rows": json.loads(r["data"])} if r else None

    def get_quotes(self) -> Dict[str, Dict[str, Any]]:
        """Quotes the arb bot has resting (or had, if it stopped without cancelling)."""
        return {r["race"]: json.loads(r["data"]) for r in self.conn.execute("SELECT * FROM arb_quotes")}

    def save_quote(self, race: str, data: Dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO arb_quotes VALUES (?,?,?)", (race, json.dumps(data), now_iso()))
        self.conn.commit()

    def delete_quote(self, race: str) -> None:
        self.conn.execute("DELETE FROM arb_quotes WHERE race=?", (race,))
        self.conn.commit()

    def set_alive(self, cycle_started: Optional[float], cycle: int, mode: str, starting: bool = False) -> None:
        """Written every few seconds from a separate thread, so the dashboard can tell a bot stuck
        in a slow cycle (or a slow startup) from a stopped one."""
        self.conn.execute("INSERT OR REPLACE INTO bot_alive (id, ts, cycle_started, cycle, mode, starting) "
                          "VALUES (1, ?, ?, ?, ?, ?)", (now_iso(), cycle_started, cycle, mode, int(starting)))
        self.conn.commit()

    def get_alive(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM bot_alive WHERE id = 1").fetchone()
        return dict(r) if r else None

    def get_status(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT ts, status FROM bot_status WHERE id = 1").fetchone()
        return {"ts": r["ts"], **json.loads(r["status"])} if r else None

    def query(self, sql: str, params: tuple = ()) -> list:
        return self.conn.execute(sql, params).fetchall()

    def outcome(self, market_id: str) -> Optional[str]:
        r = self.conn.execute("SELECT settled_with FROM outcomes WHERE market_id=?", (market_id,)).fetchone()
        return r[0] if r else None
