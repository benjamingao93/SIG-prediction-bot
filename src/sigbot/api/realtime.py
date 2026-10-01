"""Realtime order-book feed (Supabase private broadcast). Optional: `pip install -e '.[realtime]'`.

Realtime doesn't count against the REST budget, so it's the way to watch many books at once.
It is best-effort with no replay: on subscribe, reconnect, token refresh, a revision gap, or a
`resyncRequired` batch, the caller must refetch books over REST (the `on_resync` callback).

NOTE: written against the documented contract but not yet exercised against the live feed.
Run `sigbot collect --realtime` and watch the log before relying on it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Iterable, List

from .client import SigClient
from .models import OrderBook

log = logging.getLogger(__name__)
TOKEN_REFRESH_SECONDS = 2.5 * 3600  # tokens live 3h


def _iter_books(books: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(books, dict):
        for ex_id, b in books.items():
            if isinstance(b, dict):
                yield {"exchangeId": b.get("exchangeId", ex_id), **b}
    elif isinstance(books, list):
        yield from (b for b in books if isinstance(b, dict))


class RealtimeBooks:
    def __init__(
        self,
        client: SigClient,
        tournament_id: str,
        market_ids: List[str],
        on_book: Callable[[OrderBook], None],
        on_resync: Callable[[str], None],
        on_settled: Callable[[Dict[str, Any]], None] = lambda item: None,
    ):
        self.client = client
        self.tournament_id = tournament_id
        self.market_ids = market_ids
        self.on_book = on_book
        self.on_resync = on_resync
        self.on_settled = on_settled
        self._last_rev: Dict[str, int] = {}

    def _handle(self, market_id: str, payload: Dict[str, Any]) -> None:
        # supabase-py may hand us {event, payload, type} or the payload itself
        if "payload" in payload and isinstance(payload["payload"], dict):
            payload = payload["payload"]
        delivery = payload.get("delivery") or {}
        rev, prev = delivery.get("revision"), delivery.get("previousRevision")
        last = self._last_rev.get(market_id)

        gap = last is not None and prev is not None and prev > last
        if gap or payload.get("resyncRequired"):
            log.warning("realtime gap/resync on market %s (prev=%s last=%s)", market_id, prev, last)
            self.on_resync(market_id)

        # Books are versioned snapshots and always apply, even on duplicate batches.
        for b in _iter_books(payload.get("books")):
            try:
                self.on_book(OrderBook.from_api({"marketId": market_id, **b}))
            except (KeyError, TypeError, ValueError) as e:
                log.debug("skip malformed book: %s", e)

        if rev is not None and (last is None or rev > last):
            self._last_rev[market_id] = rev
            for item in payload.get("marketSettled") or []:
                self.on_settled(item)

    async def run(self) -> None:
        try:
            from supabase import acreate_client
        except ImportError as e:
            raise SystemExit("Realtime needs: pip install -e '.[realtime]'") from e

        while True:
            tok = self.client.post("/realtime/token")
            sb = await acreate_client(tok["supabaseUrl"], tok["anonKey"])
            await sb.realtime.set_auth(tok["token"])
            channels = []
            for mid in self.market_ids:
                ch = sb.channel(f"tournament:{self.tournament_id}:market:{mid}", {"config": {"private": True}})
                ch.on_broadcast("market_batch", lambda p, mid=mid: self._handle(mid, p))
                await ch.subscribe()
                channels.append(ch)
                self.on_resync(mid)  # establish initial state over REST
            log.info("realtime: subscribed to %d markets", len(channels))
            await asyncio.sleep(TOKEN_REFRESH_SECONDS)
            for ch in channels:
                await sb.remove_channel(ch)
            self._last_rev.clear()
