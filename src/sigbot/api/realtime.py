"""Realtime order books for tournament markets (Supabase private broadcast).
Needs `pip install -e '.[realtime]'`.

The feed pushes a `market_batch` per market at most once a second, and on tournament topics each
batch carries the complete, versioned book of every exchange it names. Pushes don't count against
the 100 reads/minute, so the bot can hold every book current without polling. The contract
(https://sig.thesuper.market/api/v1/docs, Realtime):

- Books carry `asOf {sequence, at}`; apply one only if it is newer than the book held. Books
  apply even in a duplicate batch: a version never goes backwards.
- Each batch has `delivery {revision, previousRevision}`. revision <= last accepted: duplicate
  (seen live: the same batch arrives up to three times). previousRevision > last accepted:
  batches were missed, reload that market over REST.
- Also reload on subscribe, reconnect, token refresh, socket error, `resyncRequired`, a trade
  whose `sequence` is null, and after a book's `nextExpiryAt` (expiries emit nothing).
- Tokens last 3 hours.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

from .client import SigAPIError, SigClient
from .models import OrderBook

log = logging.getLogger(__name__)
TOKEN_REFRESH_SECONDS = 2.5 * 3600  # tokens live 3h
STALL_SECONDS = 90.0  # no batch from any market this long: the socket is dead, rebuild it
JOIN_BATCH = 20  # channels joined at a time: 237 at once timed out, one at a time took minutes
JOIN_WAIT = 12.0  # seconds to wait for a batch's join replies (the library times out at 10)
JOIN_RETRIES = 2

Version = Tuple[int, float]  # (engine sequence, engine time as epoch seconds)


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    # the engine sends 7 fractional digits; Python 3.9's fromisoformat takes at most 6
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _version(as_of: Any) -> Optional[Version]:
    if not isinstance(as_of, dict) or as_of.get("sequence") is None:
        return None
    return int(as_of["sequence"]), _epoch(as_of.get("at")) or 0.0


class BookStore:
    """Latest known book per exchange, applied strictly by version."""

    def __init__(self) -> None:
        self.books: Dict[str, OrderBook] = {}
        self.versions: Dict[str, Optional[Version]] = {}
        self.market_of: Dict[str, str] = {}
        self.expiry: Dict[str, float] = {}  # exchange → epoch of its soonest resting-order expiry

    def apply(self, market_id: str, raw: Dict[str, Any], authoritative: bool = False) -> bool:
        """True if the book replaced what we held. A REST read (authoritative) with no version
        always applies; a versioned book applies only when newer."""
        ex = str(raw["exchangeId"])
        v = _version(raw.get("asOf"))
        if ex in self.versions:
            held = self.versions[ex]
            if v is None and not authoritative:
                return False
            if v is not None and held is not None and v <= held:
                return False
        self.books[ex] = OrderBook.from_api({**raw, "exchangeId": ex, "marketId": market_id})
        self.versions[ex] = v
        self.market_of[ex] = market_id
        exp = _epoch(raw.get("nextExpiryAt"))
        if exp is None:
            self.expiry.pop(ex, None)
        else:
            self.expiry[ex] = exp
        return True

    def age(self, exchange_id: str, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the engine read this book (None if unversioned)."""
        v = self.versions.get(exchange_id)
        return None if v is None or not v[1] else (now or time.time()) - v[1]

    def quotes(self) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
        return {ex: (b.best_bid, b.best_ask) for ex, b in self.books.items()}

    def expired_markets(self, now: Optional[float] = None) -> Set[str]:
        """Markets with a book past its nextExpiryAt: its resting orders have expired silently."""
        now = now or time.time()
        due = {ex for ex, t in self.expiry.items() if t <= now}
        for ex in due:
            del self.expiry[ex]
        return {self.market_of[ex] for ex in due if ex in self.market_of}


class MarketFeed:
    """Revision tracking per market topic. handle() applies a batch and says what changed."""

    def __init__(self, store: BookStore) -> None:
        self.store = store
        self.last_rev: Dict[str, int] = {}
        self.need_resync: Set[str] = set()
        self.stats: Counter = Counter()

    def handle(self, market_id: str, msg: Dict[str, Any]) -> Set[str]:
        p = msg.get("payload", msg) if isinstance(msg, dict) else {}
        self.stats["batches"] += 1
        changed = {str(b["exchangeId"]) for b in p.get("books") or []
                   if isinstance(b, dict) and self.store.apply(market_id, b)}
        d = p.get("delivery") or {}
        rev, prev = d.get("revision"), d.get("previousRevision")
        last = self.last_rev.get(market_id)
        if p.get("resyncRequired"):
            self.stats["resync_required"] += 1
            self.need_resync.add(market_id)
            if rev is not None and (last is None or rev > last):
                self.last_rev[market_id] = rev
            return changed
        if last is not None and rev is not None and rev <= last:
            self.stats["duplicates"] += 1
            return changed
        if last is not None and prev is not None and prev > last:
            self.stats["gaps"] += 1
            self.need_resync.add(market_id)
        if any(t.get("sequence") is None for t in p.get("trades") or []):
            self.need_resync.add(market_id)
        if rev is not None:
            self.last_rev[market_id] = rev
        for s in p.get("marketSettled") or []:
            log.info("market %s settled: %s", s.get("marketId"), s.get("settledWith"))
        return changed


class FeedRunner:
    """Subscribes to every market, keeps the BookStore current, and calls on_change(exchange_ids)
    after each update. REST reloads use the client's read budget and leave `reserve` reads free."""

    def __init__(self, client: SigClient, tournament_id: str, market_ids: Iterable[str],
                 on_change: Callable[[Set[str]], None] = lambda exs: None, reserve: int = 3):
        self.client = client
        self.tid = tournament_id
        self.market_ids = list(market_ids)
        self.store = BookStore()
        self.feed = MarketFeed(self.store)
        self.on_change = on_change
        self.reserve = reserve
        self.subscribed: Set[str] = set()
        self.last_batch = time.monotonic()

    def resync(self, market_id: str) -> Set[str]:
        """Authoritative reload of one market's books over REST (one read)."""
        resp = self.client.get(f"/markets/{market_id}/orderbook", tournamentId=self.tid, depth=200)
        changed = set()
        for b in resp.get("exchanges", []):
            if self.store.apply(market_id, b, authoritative=True):
                changed.add(str(b["exchangeId"]))
        self.feed.stats["resyncs"] += 1
        return changed

    def _on_batch(self, market_id: str, msg: Dict[str, Any]) -> None:
        self.last_batch = time.monotonic()
        try:
            changed = self.feed.handle(market_id, msg)
            if changed:
                self.on_change(changed)
        except Exception:
            log.exception("bad batch on market %s", market_id)

    def _on_state(self, market_id: str, state: Any, err: Any = None) -> None:
        name = str(state).rsplit(".", 1)[-1]
        if name == "SUBSCRIBED":
            self.subscribed.add(market_id)
        else:  # CHANNEL_ERROR, TIMED_OUT, CLOSED: we may have missed batches
            self.subscribed.discard(market_id)
            self.feed.need_resync.add(market_id)
            self.feed.stats[f"state_{name.lower()}"] += 1
            if err:
                log.warning("market %s channel %s: %s", market_id, name, err)

    async def _mint(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.client.post, "/realtime/token")

    async def _work_resyncs(self) -> None:
        self.feed.need_resync |= self.store.expired_markets()
        while self.feed.need_resync and self.client.reads.available > self.reserve:
            mid = self.feed.need_resync.pop()
            try:
                changed = await asyncio.to_thread(self.resync, mid)
            except SigAPIError as e:
                log.warning("resync %s failed: %s", mid, e)
                self.feed.need_resync.add(mid)
                return
            if changed:
                self.on_change(changed)

    async def _join_all(self, sb) -> None:
        """Join every market's channel in batches, waiting for each batch's replies, then retry
        the ones that didn't join on fresh channels (a channel can only subscribe once)."""
        channels: Dict[str, Any] = {}
        todo = list(self.market_ids)
        for attempt in range(1 + JOIN_RETRIES):
            for i in range(0, len(todo), JOIN_BATCH):
                batch = todo[i:i + JOIN_BATCH]
                for mid in batch:
                    old = channels.pop(mid, None)
                    if old is not None:
                        try:
                            await sb.remove_channel(old)
                        except Exception:
                            pass
                    ch = sb.channel(f"tournament:{self.tid}:market:{mid}", {"config": {"private": True}})
                    ch.on_broadcast("market_batch", lambda m, mid=mid: self._on_batch(mid, m))
                    channels[mid] = ch
                await asyncio.gather(*(channels[mid].subscribe(lambda st, err=None, mid=mid: self._on_state(mid, st, err))
                                       for mid in batch))
                deadline = time.monotonic() + JOIN_WAIT
                while time.monotonic() < deadline and any(mid not in self.subscribed for mid in batch):
                    await asyncio.sleep(0.1)
            todo = [mid for mid in self.market_ids if mid not in self.subscribed]
            if not todo:
                return
            self.feed.stats["join_retries"] += len(todo)
            log.warning("realtime: %d markets didn't join; retrying (%d/%d)", len(todo), attempt + 1, JOIN_RETRIES)
        log.error("realtime: %d markets never joined: %s", len(todo), todo[:10])

    async def run(self) -> None:
        try:
            from supabase import acreate_client
        except ImportError as e:
            raise SystemExit("Realtime needs: pip install -e '.[realtime]'") from e
        backoff = 1.0
        while True:
            sb = None
            try:
                tok = await self._mint()
                sb = await acreate_client(tok["supabaseUrl"], tok["anonKey"])
                await sb.realtime.set_auth(tok["token"])
                await self._join_all(sb)
                self.feed.need_resync |= set(self.market_ids)  # initial state comes from REST
                log.info("realtime: subscribed to %d/%d markets", len(self.subscribed), len(self.market_ids))
                backoff = 1.0
                self.last_batch = time.monotonic()
                refresh_at = time.monotonic() + TOKEN_REFRESH_SECONDS
                while True:
                    # The library's own reconnect can die silently (seen live: a keepalive
                    # timeout, then rejoining nothing), so watch the socket and the traffic.
                    if not sb.realtime.is_connected:
                        raise ConnectionError("socket closed")
                    if time.monotonic() - self.last_batch > STALL_SECONDS:
                        raise ConnectionError(f"no batch from any market for {STALL_SECONDS:.0f}s")
                    await self._work_resyncs()
                    if time.monotonic() > refresh_at:
                        tok = await self._mint()
                        await sb.realtime.set_auth(tok["token"])
                        self.feed.need_resync |= set(self.market_ids)
                        self.feed.stats["token_refreshes"] += 1
                        refresh_at = time.monotonic() + TOKEN_REFRESH_SECONDS
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.feed.stats["reconnects"] += 1
                log.warning("realtime connection lost (%s): reconnecting in %.0fs", e, backoff)
                self.subscribed.clear()
                self.feed.last_rev.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                if sb is not None:
                    try:
                        await sb.realtime.close()
                    except Exception:
                        pass
