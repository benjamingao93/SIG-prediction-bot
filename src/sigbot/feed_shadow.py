"""`sigbot feed`: run the realtime book feed in listen-only mode. Sends no orders.

Keeps every tournament book current from the WebSocket feed and, on each update, runs the arb
bot's own screens (buy and exit) against the pushed books, logging what it would have traded and
how old the books were. Once a minute it reads one book over REST and compares it with the feed's
copy at the same engine version, then logs a summary. This is the evidence for switching the bot
to the feed: books that match REST, few gaps, and opportunities seen on fresher prices.
"""
from __future__ import annotations

import asyncio
import logging
import random
import statistics
import time
from collections import Counter, deque
from typing import Dict, List, Optional, Set

from .api import markets as mk
from .api import portfolio as pf
from .api.client import SigAPIError, SigClient, call_patiently
from .api.realtime import FeedRunner, _version
from .config import Settings
from .trading import arb

log = logging.getLogger(__name__)


class Shadow:
    def __init__(self, s: Settings, client: SigClient):
        self.s, self.client, self.cfg = s, client, s.arb
        t = call_patiently(client, lambda: mk.get_tournament(client, s.tournament_slug), "reading the tournament")
        self.tid = t["id"]
        ms = call_patiently(client, lambda: mk.list_tournament_markets(client, s.tournament_slug, status="open"),
                            "loading markets")
        self.baskets = arb.build_baskets(ms)
        self.by_exchange: Dict[str, List[arb.Basket]] = {}
        for b in self.baskets:
            for l in b.legs:
                self.by_exchange.setdefault(l.exchange_id, []).append(b)
        market_ids = sorted({m.id for m in ms if m.is_binary})
        self.runner = FeedRunner(client, self.tid, market_ids, on_change=self.on_change)
        self.held: Dict[str, int] = {}
        self.held_cost: Dict[str, float] = {}
        self.stats: Counter = Counter()
        self.ages: deque = deque(maxlen=500)  # engine-time age of books as they arrive
        self._logged: Dict[tuple, float] = {}

    # ---- opportunities on every update ----

    def on_change(self, exchange_ids: Set[str]) -> None:
        store = self.runner.store
        now = time.time()
        for ex in exchange_ids:
            a = store.age(ex, now)
            if a is not None:
                self.ages.append(a)
        quotes = store.quotes()
        for b in {b for ex in exchange_ids for b in self.by_exchange.get(ex, [])}:
            if any(l.exchange_id not in store.books for l in b.legs):
                continue
            books = {l.exchange_id: store.books[l.exchange_id] for l in b.legs}
            age = max((store.age(l.exchange_id, now) or 0.0) for l in b.legs)
            side = arb.screen(b, quotes, self.cfg.min_profit, self.cfg.allow_yes)
            if side:
                o = arb.size(b, side, books, self.cfg.min_profit, self.cfg.max_sets, self.cfg.depth_fraction)
                if o and self._fresh_log(("buy", b.key, o.sets)):
                    self.stats["would_buy"] += 1
                    log.info("WOULD BUY  %s %s×%d sets=%d profit=%+.2f books %.1fs old",
                             b.key, side.upper(), len(b.legs), o.sets, o.profit, age)
            if self.cfg.exit_enabled and b.key in self.held:
                cost = self.held_cost.get(b.key)
                bar = arb.exit_bar(b, cost, self.cfg.min_profit,
                                   self.cfg.exit_min_profit if self.cfg.exit_early else None)
                if arb.screen_exit(b, quotes, bar) is not None:
                    o = arb.size_exit(b, books, self.held[b.key], bar, self.cfg.max_sets, cost, self.cfg.depth_fraction)
                    if o and self._fresh_log(("exit", b.key, o.sets)):
                        self.stats["would_exit"] += 1
                        log.info("WOULD EXIT %s sets=%d profit=%+.2f (vs holding %+.2f) books %.1fs old",
                                 b.key, o.sets, o.realized if o.realized is not None else o.gain, o.gain, age)

    def _fresh_log(self, key: tuple, every: float = 30.0) -> bool:
        """Log a standing opportunity once per 30 s, not on every update."""
        now = time.monotonic()
        if now - self._logged.get(key, -1e9) < every:
            return False
        self._logged[key] = now
        return True

    # ---- periodic checks ----

    def refresh_held(self) -> None:
        no_pos = [p for p in pf.positions(self.client, self.s.tournament_slug) if p.side == "no" and not p.settled]
        self.held = arb.held_baskets(self.baskets, {p.exchange_id: abs(p.quantity) for p in no_pos})
        avg = {p.exchange_id: p.avg_cost for p in no_pos}
        self.held_cost = {b.key: sum(avg.get(l.exchange_id, 0.0) for l in b.legs)
                          for b in self.baskets if b.key in self.held}

    def spot_check(self) -> None:
        """Compare one feed book with a REST read of it. Same version: must match exactly."""
        store = self.runner.store
        synced = [ex for ex, v in store.versions.items() if v is not None]
        if not synced or self.client.reads.available <= self.runner.reserve + 1:
            return
        ex = random.choice(synced)
        raw = self.client.get(f"/exchanges/{ex}/orderbook", tournamentId=self.tid, depth=200)
        rest_v, feed_v = _version(raw.get("asOf")), store.versions.get(ex)
        if rest_v is None or feed_v is None:
            self.stats["check_unversioned"] += 1
        elif rest_v[0] == feed_v[0]:
            fb = store.books[ex]
            same = ([(l["price"], l["quantity"]) for l in raw.get("bids", [])] == [(l.price, l.quantity) for l in fb.bids]
                    and [(l["price"], l["quantity"]) for l in raw.get("asks", [])] == [(l.price, l.quantity) for l in fb.asks])
            self.stats["check_match" if same else "check_mismatch"] += 1
            if not same:
                log.warning("feed book for %s differs from REST at the same version %s", ex, rest_v[0])
        elif rest_v[0] > feed_v[0]:
            self.stats["check_feed_behind"] += 1
            log.info("feed behind REST on %s by %.1fs of engine time", ex, rest_v[1] - feed_v[1])
        else:
            self.stats["check_feed_ahead"] += 1

    def summary(self) -> str:
        f, r = self.runner.feed.stats, self.runner
        synced = sum(1 for v in r.store.versions.values() if v is not None)
        ages = sorted(self.ages)
        med = f"{statistics.median(ages):.1f}s" if ages else "–"
        p90 = f"{ages[int(len(ages) * .9)]:.1f}s" if ages else "–"
        checks = {k[6:]: v for k, v in self.stats.items() if k.startswith("check_")}
        return (f"feed: subscribed {len(r.subscribed)}/{len(r.market_ids)} markets, books {synced} synced, "
                f"{len(r.feed.need_resync)} awaiting REST | batches {f['batches']} dup {f['duplicates']} "
                f"gaps {f['gaps']} resyncs {f['resyncs']} reconnects {f['reconnects']} | book age median {med} "
                f"p90 {p90} | REST checks {checks or '–'} | would buy {self.stats['would_buy']} "
                f"exit {self.stats['would_exit']} | held baskets {len(self.held)}")

    async def checks(self) -> None:
        last_held = 0.0
        while True:
            await asyncio.sleep(60)
            try:
                if time.monotonic() - last_held > 300 and self.client.reads.available > self.runner.reserve + 1:
                    await asyncio.to_thread(self.refresh_held)
                    last_held = time.monotonic()
                await asyncio.to_thread(self.spot_check)
            except SigAPIError as e:
                log.warning("check failed: %s", e)
            log.info(self.summary())

    async def run(self, minutes: Optional[float] = None) -> None:
        try:
            self.refresh_held()
        except SigAPIError as e:
            log.warning("positions unavailable, exits not screened yet: %s", e)
        tasks = [asyncio.ensure_future(self.runner.run()), asyncio.ensure_future(self.checks())]
        try:
            if minutes:
                await asyncio.sleep(minutes * 60)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("final " + self.summary())


def run_shadow(s: Settings, read_budget: int, minutes: Optional[float]) -> None:
    client = SigClient(s.api_key, s.base_url, read_budget=read_budget, write_budget=2)
    asyncio.run(Shadow(s, client).run(minutes))
