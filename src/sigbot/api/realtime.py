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
import random
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .client import SigAPIError, SigClient
from .models import OrderBook

log = logging.getLogger(__name__)
TOKEN_REFRESH_SECONDS = 2.5 * 3600  # tokens live 3h
# Joins are slow on the server: 3.5-6 s each (measured, public and private channels alike), and a
# batch queues behind itself. The library's default 10 s join timeout failed most of a batch of 20.
JOIN_TIMEOUT = 60  # seconds the library waits for a join reply (its default is 10)
JOIN_BATCH = 10  # channels joined at a time: a batch fits inside JOIN_TIMEOUT at ~5 s per join
JOIN_WAIT = JOIN_TIMEOUT + 5.0  # seconds we wait for a batch's join replies
JOIN_RETRY_AFTER = 30.0  # seconds before retrying a market that wouldn't join
VERIFY_LAG = 5.0  # seconds of engine time a feed book may trail REST before it's reloaded
# Each batch is sent once and never retried, so the last batch on a quiet market can be dropped with
# no later gap to reveal it. The docs say to refetch periodically (every one to two minutes): each
# watched market is reloaded over REST every RESYNC_EVERY, and its books are trusted only while
# the last reload is under TRUST_FOR old.
RESYNC_EVERY = 90.0
TRUST_FOR = 150.0

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

    def drop_market(self, market_id: str) -> None:
        for ex in [ex for ex, m in self.market_of.items() if m == market_id]:
            for d in (self.books, self.versions, self.market_of, self.expiry):
                d.pop(ex, None)

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
    """Keeps the BookStore current for a watchlist of markets and calls on_change(exchange_ids)
    after each update. The watchlist can change while running (set_watch); markets are joined and
    left to match it. usable(exchange_id) says whether a book can be trusted right now: an
    unchanged book is still current as long as its market is subscribed with nothing missed.
    REST reloads use the client's read budget and leave `reserve` reads free.

    Health: the client library's own reconnect can die silently (seen live: a keepalive timeout,
    then "rejoining" nothing), so the runner watches the socket itself and, every verify_every
    seconds, reads one watched book over REST: a feed copy more than VERIFY_LAG seconds of engine
    time behind gets reloaded, and two in a row rebuild the connection."""

    def __init__(self, client: SigClient, tournament_id: str, market_ids: Iterable[str] = (),
                 on_change: Callable[[Set[str]], None] = lambda exs: None, reserve: int = 3,
                 verify_every: float = 60.0):
        self.client = client
        self.tid = tournament_id
        self.store = BookStore()
        self.feed = MarketFeed(self.store)
        self.on_change = on_change
        self.reserve = reserve
        self.verify_every = verify_every
        self._desired: Set[str] = set(market_ids)
        self.joined: Dict[str, Any] = {}  # market → channel on the current connection
        self.subscribed: Set[str] = set()
        self.healthy = False
        self._join_after: Dict[str, float] = {}  # market → monotonic time to retry a failed join
        self.synced_at: Dict[str, float] = {}  # market → monotonic time of its last REST reload
        self._behind = 0
        # Your account channel (user:{profile_id}): fills pushed within ~1 s. Pushed fills carry
        # no id, so they are a signal to read the fills list, not something to count directly.
        self.user_subscribed = False
        self.user_need_poll = True  # set on (re)join, gap, resync and token refresh
        self._user_rev: Optional[int] = None
        self._fills: deque = deque()
        self.on_fills: Callable[[], None] = lambda: None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- watchlist and trust ----

    @property
    def market_ids(self) -> List[str]:
        return sorted(self._desired)

    def set_watch(self, market_ids: Iterable[str]) -> None:
        self._desired = set(market_ids)  # one assignment: safe to call from the bot's thread

    def usable(self, exchange_id: str, now: Optional[float] = None) -> bool:
        if not self.healthy:
            return False
        m = self.store.market_of.get(exchange_id)
        if m is None or m not in self.subscribed or m in self.feed.need_resync:
            return False
        if time.monotonic() - self.synced_at.get(m, -1e9) > TRUST_FOR:
            return False  # overdue for its periodic reload: a dropped batch could be hiding
        if self.store.versions.get(exchange_id) is None:
            return False
        exp = self.store.expiry.get(exchange_id)
        return exp is None or exp > (now or time.time())

    def user_trusted(self) -> bool:
        """Pushed fills can be relied on: nothing missed since the last fills-list read."""
        return self.healthy and self.user_subscribed and not self.user_need_poll

    def pop_fills(self) -> List[Dict[str, Any]]:
        out = []
        while self._fills:
            out.append(self._fills.popleft())
        return out

    def _on_account(self, msg: Dict[str, Any]) -> None:
        p = msg.get("payload", msg) if isinstance(msg, dict) else {}
        d = p.get("delivery") or {}
        rev, prev = d.get("revision"), d.get("previousRevision")
        last = self._user_rev
        if p.get("resyncRequired"):
            self.user_need_poll = True
        elif last is not None and rev is not None and rev <= last:
            return  # duplicate
        elif last is not None and prev is not None and prev > last:
            self.feed.stats["user_gaps"] += 1
            self.user_need_poll = True
        if rev is not None and (last is None or rev > last):
            self._user_rev = rev
        fills = [f for f in p.get("fills") or [] if isinstance(f, dict)]
        if fills:
            self.feed.stats["pushed_fills"] += len(fills)
            self._fills.extend(fills)
            self.on_fills()

    # ---- REST ----

    def resync(self, market_id: str) -> Set[str]:
        """Authoritative reload of one market's books over REST (one read)."""
        resp = self.client.get(f"/markets/{market_id}/orderbook", tournamentId=self.tid, depth=200)
        changed = set()
        for b in resp.get("exchanges", []):
            if self.store.apply(market_id, b, authoritative=True):
                changed.add(str(b["exchangeId"]))
        self.synced_at[market_id] = time.monotonic()
        self.feed.stats["resyncs"] += 1
        return changed

    def _verify_one(self) -> Optional[bool]:
        """Compare one usable watched book with REST. True: feed behind. None: nothing checked."""
        exs = [ex for ex in list(self.store.versions) if self.usable(ex)]
        if not exs or self.client.reads.available <= self.reserve:
            return None
        ex = random.choice(exs)
        raw = self.client.get(f"/exchanges/{ex}/orderbook", tournamentId=self.tid, depth=1)
        rest_v, feed_v = _version(raw.get("asOf")), self.store.versions.get(ex)
        self.feed.stats["verifies"] += 1
        if rest_v is None or feed_v is None:
            return None
        behind = rest_v[0] > feed_v[0] and rest_v[1] - feed_v[1] > VERIFY_LAG
        if behind:
            self.feed.stats["verify_behind"] += 1
            m = self.store.market_of.get(ex)
            if m:
                self.feed.need_resync.add(m)
            log.warning("feed book for %s is %.0fs behind REST: reloading", ex, rest_v[1] - feed_v[1])
        return behind

    # ---- callbacks (on the feed's event loop) ----

    def _on_batch(self, market_id: str, msg: Dict[str, Any]) -> None:
        if market_id not in self.joined:
            return  # a market we've just left
        try:
            changed = self.feed.handle(market_id, msg)
            if changed:
                self.on_change(changed)
        except Exception:
            log.exception("bad batch on market %s", market_id)

    def _on_dirty(self, market_id: str, event: str) -> None:
        if market_id in self.joined:
            self.feed.need_resync.add(market_id)
            self.feed.stats[event] += 1

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

    # ---- connection work ----

    async def _mint(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.client.post, "/realtime/token")

    async def _work_resyncs(self) -> None:
        self.feed.need_resync |= self.store.expired_markets()
        pending = [m for m in self.feed.need_resync if m in self.subscribed]
        # Periodic reconciliation, oldest first, after anything known to be stale.
        now = time.monotonic()
        due = sorted((m for m in self.subscribed if m not in self.feed.need_resync
                      and now - self.synced_at.get(m, -1e9) > RESYNC_EVERY),
                     key=lambda m: self.synced_at.get(m, -1e9))
        for m in due:
            self.feed.need_resync.add(m)
        pending += due
        for mid in pending:
            if self.client.reads.available <= self.reserve:
                return
            self.feed.need_resync.discard(mid)
            try:
                changed = await asyncio.to_thread(self.resync, mid)
            except SigAPIError as e:
                log.warning("resync %s failed: %s", mid, e)
                self.feed.need_resync.add(mid)
                return
            if changed:
                self.on_change(changed)

    async def _leave(self, sb, mid: str) -> None:
        ch = self.joined.pop(mid, None)
        self.subscribed.discard(mid)
        self.feed.last_rev.pop(mid, None)
        self.feed.need_resync.discard(mid)
        self.synced_at.pop(mid, None)
        self.store.drop_market(mid)
        if ch is not None:
            try:
                await sb.remove_channel(ch)
            except Exception:
                pass

    async def _join(self, sb, mids: List[str]) -> None:
        """Join in batches, waiting for each batch's replies (237 at once timed out; one at a
        time took minutes). A market that won't join is retried after JOIN_RETRY_AFTER."""
        for i in range(0, len(mids), JOIN_BATCH):
            batch = mids[i:i + JOIN_BATCH]
            for mid in batch:
                ch = sb.channel(f"tournament:{self.tid}:market:{mid}", {"config": {"private": True}})
                ch.on_broadcast("market_batch", lambda m, mid=mid: self._on_batch(mid, m))
                # Admin and settlement actions can still send these on their own, with no books:
                # the book changed, so reload it.
                ch.on_broadcast("book_dirty", lambda m, mid=mid: self._on_dirty(mid, "book_dirty"))
                ch.on_broadcast("market_settled", lambda m, mid=mid: self._on_dirty(mid, "market_settled"))
                self.joined[mid] = ch
            await asyncio.gather(*(self.joined[mid].subscribe(lambda st, err=None, mid=mid: self._on_state(mid, st, err))
                                   for mid in batch))
            deadline = time.monotonic() + JOIN_WAIT
            while time.monotonic() < deadline and any(mid not in self.subscribed for mid in batch):
                await asyncio.sleep(0.1)
            for mid in batch:
                if mid in self.subscribed:
                    self.feed.need_resync.add(mid)  # initial state comes from REST
                else:
                    self.feed.stats["join_failures"] += 1
                    self._join_after[mid] = time.monotonic() + JOIN_RETRY_AFTER
                    await self._leave(sb, mid)

    async def _reconcile(self, sb) -> None:
        desired = self._desired
        for mid in [m for m in self.joined if m not in desired]:
            await self._leave(sb, mid)
        now = time.monotonic()
        new = sorted(m for m in desired if m not in self.joined and self._join_after.get(m, 0) <= now)
        if new:
            await self._join(sb, new)

    def _reset(self) -> None:
        self.healthy = False
        self.user_subscribed = False
        self.user_need_poll = True
        self._user_rev = None
        self.joined.clear()
        self.subscribed.clear()
        self.feed.last_rev.clear()

    async def run(self) -> None:
        try:
            from supabase import acreate_client
        except ImportError as e:
            raise SystemExit("Realtime needs: pip install -e '.[realtime]'") from e
        backoff = 1.0
        while not self._stop.is_set():
            sb = None
            try:
                tok = await self._mint()
                sb = await acreate_client(tok["supabaseUrl"], tok["anonKey"])
                await sb.realtime.set_auth(tok["token"])
                # Connect once, explicitly. Each subscribe connects on its own if the socket isn't
                # up yet, so a batch of concurrent subscribes opened one socket each; only one was
                # read, the other joins "timed out", and the orphans died of keepalive timeouts.
                await sb.realtime.connect()
                sb.realtime.timeout = JOIN_TIMEOUT  # channels copy this when created
                self._reset()
                user_topic = (tok.get("channels") or {}).get("user")
                if user_topic:
                    uch = sb.channel(user_topic, {"config": {"private": True}})
                    uch.on_broadcast("account_batch", self._on_account)
                    await uch.subscribe(lambda st, err=None: setattr(
                        self, "user_subscribed", str(st).rsplit(".", 1)[-1] == "SUBSCRIBED"))
                await self._reconcile(sb)
                # The library reconnects on its own after a keepalive timeout but rejoins nothing
                # (seen live), leaving channels we think are live on a socket that isn't theirs.
                # Remember this socket: if it's ever replaced, rebuild everything ourselves.
                socket = getattr(sb.realtime, "_ws_connection", None)
                self.healthy = True
                log.info("realtime: subscribed to %d/%d markets", len(self.subscribed), len(self._desired))
                backoff = 1.0
                refresh_at = time.monotonic() + TOKEN_REFRESH_SECONDS
                verify_at = time.monotonic() + self.verify_every
                while not self._stop.is_set():
                    if not sb.realtime.is_connected:
                        raise ConnectionError("socket closed")
                    if getattr(sb.realtime, "_ws_connection", None) is not socket:
                        raise ConnectionError("socket replaced by the library's reconnect")
                    await self._reconcile(sb)
                    await self._work_resyncs()
                    if time.monotonic() >= verify_at:
                        verify_at = time.monotonic() + self.verify_every
                        try:
                            behind = await asyncio.to_thread(self._verify_one)
                        except SigAPIError as e:
                            log.info("feed verify skipped: %s", e)
                            behind = None
                        if behind is not None:
                            self._behind = self._behind + 1 if behind else 0
                            if self._behind >= 2:
                                self._behind = 0
                                raise ConnectionError("feed fell behind REST twice in a row")
                    if time.monotonic() > refresh_at:
                        tok = await self._mint()
                        await sb.realtime.set_auth(tok["token"])
                        self.feed.need_resync |= set(self.subscribed)
                        self.user_need_poll = True
                        self.feed.stats["token_refreshes"] += 1
                        refresh_at = time.monotonic() + TOKEN_REFRESH_SECONDS
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reset()
                if self._stop.is_set():
                    break
                self.feed.stats["reconnects"] += 1
                log.warning("realtime connection lost (%s): reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                self.healthy = False
                if sb is not None:
                    try:
                        await sb.realtime.close()
                    except Exception:
                        pass

    # ---- running next to synchronous code ----

    def start_in_thread(self) -> None:
        """Run the feed on its own event loop in a daemon thread (the arb bot is synchronous)."""
        self._thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True, name="realtime-feed")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
