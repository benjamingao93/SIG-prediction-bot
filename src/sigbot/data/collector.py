"""Stage 1: record markets, prices and order books.

REST mode works inside the read budget: one bulk-prices read covers 100 exchanges, and the
remaining reads rotate through full order books, prioritizing markets whose price moved.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from ..api import markets as mk
from ..api.client import SigClient
from ..api.models import Market, OrderBook
from .db import DB

log = logging.getLogger(__name__)


class Collector:
    def __init__(
        self,
        client: SigClient,
        db: DB,
        slug: str,
        tournament_id: str,
        price_interval: float = 15.0,
        market_refresh_interval: float = 300.0,
        books_per_cycle: int = 8,
    ):
        self.client = client
        self.db = db
        self.slug = slug
        self.tournament_id = tournament_id
        self.price_interval = price_interval
        self.market_refresh_interval = market_refresh_interval
        self.books_per_cycle = books_per_cycle

        self.markets: Dict[str, Market] = {}
        self.books: Dict[str, OrderBook] = {}  # by exchange id, latest known
        self.dirty: set = set()  # exchange ids with a book change since last evaluation
        self._last_prices: Dict[str, tuple] = {}
        self._rotation: List[str] = []
        self._last_market_refresh = 0.0

    # --- markets -------------------------------------------------------------------------
    def refresh_markets(self) -> None:
        all_markets = mk.list_tournament_markets(self.client, self.slug)
        self.db.upsert_markets(all_markets)
        self.markets = {m.id: m for m in all_markets if m.status == "open" and m.is_binary}
        self._last_market_refresh = time.monotonic()
        log.info("markets: %d open binary of %d total", len(self.markets), len(all_markets))

    def exchange_ids(self) -> List[str]:
        return [m.yes_exchange_id for m in self.markets.values() if m.yes_exchange_id]

    # --- data ---------------------------------------------------------------------------
    def snapshot_prices(self) -> List[str]:
        """Returns exchange ids whose top of book changed since last snapshot."""
        prices = mk.bulk_prices(self.client, self.exchange_ids(), self.tournament_id)
        self.db.insert_prices(prices)
        moved = []
        for p in prices:
            key = (p.get("bestBid"), p.get("bestAsk"), p.get("latestPrice"))
            if self._last_prices.get(p["exchangeId"]) != key:
                moved.append(p["exchangeId"])
            self._last_prices[p["exchangeId"]] = key
        return moved

    def fetch_book(self, exchange_id: str) -> OrderBook:
        book = mk.get_orderbook(self.client, exchange_id, self.tournament_id)
        self.set_book(book)
        return book

    def set_book(self, book: OrderBook) -> None:
        prev = self.books.get(book.exchange_id)
        if prev is not None and book.as_of_seq is not None and prev.as_of_seq is not None \
                and book.as_of_seq < prev.as_of_seq:
            return  # stale
        self.books[book.exchange_id] = book
        self.dirty.add(book.exchange_id)
        self.db.insert_book(book)

    def step(self) -> None:
        if time.monotonic() - self._last_market_refresh > self.market_refresh_interval:
            self.refresh_markets()
        moved = self.snapshot_prices()
        # moved books first, then round-robin the rest so every book is refreshed eventually
        queue = list(moved)
        if not self._rotation:
            self._rotation = self.exchange_ids()
        while len(queue) < self.books_per_cycle and self._rotation:
            e = self._rotation.pop(0)
            if e not in queue:
                queue.append(e)
        for ex_id in queue[: self.books_per_cycle]:
            self.fetch_book(ex_id)

    def run(self, max_cycles: Optional[int] = None) -> None:
        self.refresh_markets()
        n = 0
        while max_cycles is None or n < max_cycles:
            t0 = time.monotonic()
            try:
                self.step()
            except Exception:  # keep collecting through transient failures
                log.exception("collector step failed")
            n += 1
            log.info("cycle %d: books=%d reads/min=%d", n, len(self.books), self.client.reads.used)
            time.sleep(max(0.0, self.price_interval - (time.monotonic() - t0)))
