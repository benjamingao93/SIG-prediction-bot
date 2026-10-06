"""Arbitrage loop: screen every race basket each cycle, size against the books, execute atomically.

Per cycle: 3 reads of bulk prices cover all 237 markets. Only baskets that clear the bar at the
top of the book get their books fetched (one read per leg). A trade is one multi-leg order
(one write), so the budget stays far inside 100 reads / 30 writes per minute.

Live execution:
  1. Read a basket's books only when the read budget can take them all at once, best edge
     first, and skip the trade anyway if they are older than ARB_MAX_BOOK_AGE by the time we'd
     send it: a stale leg rests unfilled while the others fill. A basket whose books we read
     waits basket_cooldown before we read them again, traded or not, so skips can't eat the
     budget cycle after cycle.
  2. POST /orders/multi-leg with every leg at the worst level walked: all placed or none.
  3. Cancel any leg left resting, then count its fills.
  4. If legs filled unevenly, the shortfall becomes a repair, saved in the database. Every cycle,
     before looking for new trades, the bot buys what it can of each shortfall from the latest
     book, up to a cap: break-even for the basket plus ARB_REPAIR_SLIPPAGE. A race with a repair
     pending gets no new trades. `sigbot hedge` registers a repair for a gap made any other way.
  5. Exits: a NO basket we hold pays k−1 per set at settlement; selling NO on every leg pays
     Σ(1 − ask_i) now. The bot sells once that beats holding by ARB_MIN_PROFIT, or (early exits,
     on by default) beats what the set cost by ARB_EXIT_MIN_PROFIT (same budget, freshness and
     repair handling as buys). Holdings come from
     the exchange's positions; only races held evenly on every leg count as baskets.
  Realtime (`--feed`): books for watched races come from the WebSocket feed instead of REST, so
  those trades and repairs don't wait on the rate limit (api/realtime.py).
  6. Quotes (`--quote`, quoter.py): resting buy-NO orders on one leg, hedged through repairs
     when they fill.
The kill switch blocks every order, repairs and exits included, and cancels resting quotes.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from .api import markets as mk
from .api import orders
from .api import portfolio as pf
from .api.client import SigAPIError, SigClient, call_patiently
from .api.models import OrderBook
from .data.db import DB
from .config import Settings
from .data.external.races import load_races
from .trading import arb
from .trading.arb import ArbOrder, Basket, ExitOrder

log = logging.getLogger(__name__)


class ArbBot:
    def __init__(self, s: Settings, client: SigClient, db: DB, live: bool):
        self.s, self.client, self.db, self.live, self.cfg = s, client, db, live, s.arb
        self.tournament = call_patiently(client, lambda: mk.get_tournament(client, s.tournament_slug),
                                         "reading the tournament")
        self.tid = self.tournament["id"]
        self.cash = float(self.tournament.get("myBalance") or 0)
        self.baskets: List[Basket] = []
        self.spent = 0.0
        self.repairs: Dict[str, Dict[str, Any]] = {}
        self._last_trade: Dict[str, float] = {}
        self._last_refresh = -1e9  # "long ago": time.monotonic() starts near 0 at process start on macOS
        self._quotes: Dict[str, tuple] = {}
        self.cycles = 0
        self.trades = 0
        self.stale_skips = 0
        self.exits = 0
        self.held: Dict[str, int] = {}  # race → NO sets held evenly on every leg
        self.held_cost: Dict[str, float] = {}  # race → cost per set
        self._positions_at = -1e9
        self.last_error: Optional[str] = None
        self.started = time.time()
        self.feed = None
        self.feed_hits = self.feed_misses = 0
        self._wake = threading.Event()  # set by the feed thread when your account gets fills
        self._cycle_started: Optional[float] = None  # wall time the current cycle began
        self._alive_stop = threading.Event()
        self._starting = False
        if self.cfg.feed:
            from .api.realtime import FeedRunner
            # Shares this client, so its reloads count against the same budget; keep 10 free.
            self.feed = FeedRunner(client, self.tid, reserve=10)
            self.feed.on_fills = self._wake.set
        self.mm = None
        if s.mm.enabled:
            from .market_maker import MarketMaker
            self.mm = MarketMaker(self)  # paper: simulated fills, never sends orders
        self.quoter = None
        if self.cfg.quoting and not self.cfg.exit_only:
            from .quoter import Quoter
            self.quoter = Quoter(self)
        # Fair values (Kalshi + ratings) and an edge history, when `sigbot races` and `sigbot kalshi`
        # have been run. Kalshi is fetched on its own thread and its own API, not SIG's budget.
        self.fair = None
        self._history_at = -1e9
        self._edges: List[Any] = []
        from .fairvalue_service import FairValueService
        if (s.dir.track or s.dir.directional) and FairValueService.available(s):
            self.fair = FairValueService(s)
        self.director = None
        if s.dir.directional:
            if self.fair is None:
                raise SystemExit("--directional needs fair values: run `sigbot races` and `sigbot kalshi` first.")
            from .directional import Director
            self.director = Director(self)

    # ---- setup ----

    def refresh(self) -> None:
        ms = mk.list_tournament_markets(self.client, self.s.tournament_slug, status="open")
        races = load_races(self.s.races_path) if self.cfg.allow_yes else None
        self.baskets = arb.build_baskets(ms, races)
        t = mk.get_tournament(self.client, self.s.tournament_slug)
        self.cash = float(t.get("myBalance") or 0)
        self._last_refresh = time.monotonic()
        log.info("arb: %d baskets (%d exhaustive), cash=%.0f, spent=%.0f/%.0f",
                 len(self.baskets), sum(b.exhaustive for b in self.baskets), self.cash,
                 self.spent, self.cfg.max_capital)

    def refresh_positions(self, force: bool = False) -> None:
        """The NO baskets we hold, from the exchange (authoritative). Once a minute, or after a
        trade. Uneven races and YES holdings (e.g. your own manual trades) aren't baskets."""
        if not force and time.monotonic() - self._positions_at < 60:
            return
        if not self._can_read(1):
            return
        # Directional positions sit on the same markets: take their shares and cost out first, so
        # baskets (and their exit prices) are what the arb bot actually bought.
        led_q = self.director.ledger_qty() if self.director and self.director.live else {}
        led_c = self.director.ledger_cost() if self.director and self.director.live else {}
        no_held, avg = {}, {}
        for p in pf.positions(self.client, self.s.tournament_slug):
            if p.side == "no" and not p.settled:
                q = abs(p.quantity) - led_q.get(p.exchange_id, 0.0)
                if q < 1:
                    continue
                no_held[p.exchange_id] = q
                avg[p.exchange_id] = (abs(p.cost_basis) - led_c.get(p.exchange_id, 0.0)) / q \
                    if p.exchange_id in led_q else p.avg_cost
        self.held = arb.held_baskets(self.baskets, no_held)
        self.held_cost = {b.key: sum(avg.get(l.exchange_id, 0.0) for l in b.legs)
                          for b in self.baskets if b.key in self.held}
        # What's actually in baskets now, so ARB_MAX_CAPITAL also counts baskets from earlier runs.
        self.spent = sum(self.held[r] * c for r, c in self.held_cost.items())
        self._positions_at = time.monotonic()

    def _can_read(self, n: int) -> bool:
        """Room for n reads right now, keeping 3 back for the next cycle's bulk prices."""
        return self.client.reads.available >= n + 3

    def _books(self, exchange_ids) -> Tuple[Dict[str, OrderBook], float]:
        """Fresh books and the monotonic time the first of them arrived (the age that matters).
        From the realtime feed when every leg's book there can be trusted: no reads, no wait,
        current as of now. Otherwise read over REST, in parallel so a slow exchange costs one
        round trip, not one per leg."""
        ids = list(exchange_ids)
        if self.feed is not None and all(self.feed.usable(ex) for ex in ids):
            self.feed_hits += 1
            return {ex: self.feed.store.books[ex] for ex in ids}, time.monotonic()
        if self.feed is not None:
            self.feed_misses += 1
        arrived: Dict[str, float] = {}

        def read(ex):
            book = mk.get_orderbook(self.client, ex, self.tid)
            arrived[ex] = time.monotonic()
            return book

        with ThreadPoolExecutor(max_workers=max(1, len(ids))) as pool:
            books = dict(zip(ids, pool.map(read, ids)))
        return books, min(arrived.values())

    def _update_watch(self, quotes) -> None:
        """The feed watches the races where stale books cost money: held, being repaired, quoted,
        then the ones closest to a buy or exit trigger, up to feed_markets markets."""
        by_key = {b.key: b for b in self.baskets}
        first = set(self.held) | set(self.repairs) | (set(self.quoter.active) if self.quoter else set())
        races = [r for r in sorted(first) if r in by_key]

        def closeness(b: Basket) -> float:
            qs = [quotes.get(l.exchange_id, (None, None)) for l in b.legs]
            if any(q[0] is None or q[1] is None for q in qs):
                return -1e9
            buy = sum(q[0] for q in qs) - 1  # 0 at the buy trigger, normally a little below
            spread = sum(q[1] - q[0] for q in qs)  # wide spreads: room for quotes
            return max(buy, -spread / 4)

        if self.quoter:
            from .trading import quoting
            races += [s.basket.key for s in quoting.select_quotes(self.baskets, quotes, 2 * self.cfg.quote_races,
                                                                  self.cfg.quote_edge, exclude=races)]
        races += [b.key for b in sorted(self.baskets, key=closeness, reverse=True) if b.key not in races]
        markets: List[str] = []
        for r in races:
            ms = [l.market_id for l in by_key[r].legs]
            if len(markets) + len(ms) > self.cfg.feed_markets:
                break
            markets += ms
        self.feed.set_watch(markets)

    def _stale(self, read_at: float, what: str) -> bool:
        age = time.monotonic() - read_at
        if age > self.cfg.max_book_age:
            self.stale_skips += 1
            log.warning("%s skipped: books %.1fs old (limit %.1fs)", what, age, self.cfg.max_book_age)
            return True
        return False

    # ---- one cycle ----

    def step(self) -> int:
        """Returns the number of baskets traded."""
        if time.monotonic() - self._last_refresh > 600:
            self.refresh()
        self.repairs = self.db.get_repairs()  # picks up `sigbot hedge` registrations too
        if self.live and not self.s.kill_switch.exists():
            for race in list(self.repairs):
                self.work_repair(race)
        ex_ids = [l.exchange_id for b in self.baskets for l in b.legs]
        rows = mk.bulk_prices(self.client, ex_ids, self.tid)
        quotes = arb.quotes_from_prices(rows)
        lasts = {str(r["exchangeId"]): r.get("latestPrice") for r in rows}  # last trade per market
        if self.feed is not None:
            # Feed prices are newer than the poll for the markets it watches.
            quotes.update({ex: (b.best_bid, b.best_ask) for ex, b in list(self.feed.store.books.items())
                           if self.feed.usable(ex)})
            self._update_watch(quotes)
        self._quotes = quotes
        if self.fair is not None:
            self._edges = self.fair.edges(self.baskets, quotes)
            # Only with a fresh Kalshi snapshot, so ratings-only rows don't skew `sigbot convergence`.
            if (self._edges and self.fair.fresh()
                    and time.monotonic() - self._history_at >= self.s.dir.history_every):
                self._history_at = time.monotonic()
                hist = [e.as_row() for e in self._edges]
                self.db.insert_edges_history(hist)
                self.db.set_edges(hist)
        traded = 0
        if self.cfg.exit_enabled:
            traded += self._exits(quotes)
        if self.director is not None:
            self.director.step()  # its exits always run; its entries respect --exit-only itself
        if self.mm is not None:
            self.mm.step(quotes, lasts)
        if self.cfg.exit_only or not self.cfg.buys:
            return traded  # exits and repairs (and, with --no-arb-buys, directional): no new baskets, no quotes
        if self.quoter:
            self.quoter.step(quotes, lasts)
        flagged = []
        for b in self.baskets:
            if b.key in self.repairs:
                continue
            if time.monotonic() - self._last_trade.get(b.key, -1e9) < self.cfg.basket_cooldown:
                continue
            side = arb.screen(b, quotes, self.cfg.min_profit, self.cfg.allow_yes)
            if side is not None:
                flagged.append((arb.top_edge(b, quotes, side), b, side))
        for _, b, side in sorted(flagged, key=lambda f: -f[0]):
            if not self._can_read(len(b.legs)):
                log.debug("read budget low: leaving %s for the next cycle", b.key)
                break
            self._last_trade[b.key] = time.monotonic()
            books, read_at = self._books([l.exchange_id for l in b.legs])
            order = arb.size(b, side, books, self.cfg.min_profit, self.cfg.max_sets, self.cfg.depth_fraction)
            if order is None:
                continue
            order = self._fit_budget(order, books)
            if order is None or self._stale(read_at, f"arb {b.key}"):
                continue
            if self.execute(order):
                traded += 1
        return traded

    def _exit_bar(self, b: Basket) -> float:
        early = self.cfg.exit_min_profit if self.cfg.exit_early else None
        return arb.exit_bar(b, self.held_cost.get(b.key), self.cfg.min_profit, early)

    def _exits(self, quotes) -> int:
        """Sell held baskets once selling beats holding to settlement, or (early exits) locks in
        exit_min_profit per set over what they cost."""
        self.refresh_positions()
        by_key = {b.key: b for b in self.baskets}
        flagged = []
        for race, sets in self.held.items():
            b = by_key.get(race)
            if b is None or race in self.repairs:
                continue
            if self.quoter and race in self.quoter.active:
                continue  # selling NO could run into our own resting NO bid
            if time.monotonic() - self._last_trade.get(race, -1e9) < self.cfg.basket_cooldown:
                continue
            margin = arb.screen_exit(b, quotes, self._exit_bar(b))
            if margin is not None:
                flagged.append((margin, b, sets))
        done = 0
        for _, b, sets in sorted(flagged, key=lambda f: -f[0]):
            if self.s.kill_switch.exists():
                log.warning("kill switch %s present: not exiting", self.s.kill_switch)
                break
            if not self._can_read(len(b.legs)):
                break
            self._last_trade[b.key] = time.monotonic()
            books, read_at = self._books([l.exchange_id for l in b.legs])
            order = arb.size_exit(b, books, sets, self._exit_bar(b), self.cfg.max_sets, self.held_cost.get(b.key),
                                  self.cfg.depth_fraction)
            if order is None or self._stale(read_at, f"exit {b.key}"):
                continue
            if self.execute_exit(order):
                done += 1
        return done

    def _fit_budget(self, order: ArbOrder, books) -> Optional[ArbOrder]:
        if self.s.kill_switch.exists():
            log.warning("kill switch %s present: not trading", self.s.kill_switch)
            return None
        room = min(self.cash, self.cfg.max_capital - self.spent)
        per_set = order.cost / order.sets
        if order.cost <= room:
            return order
        sets = int(room // per_set)
        if sets <= 0:
            log.info("arb %s skipped: no capital left (room %.0f)", order.basket.key, room)
            return None
        return arb.size(order.basket, order.side, books, self.cfg.min_profit, sets, self.cfg.depth_fraction)

    # ---- execution ----

    def _log(self, order: ArbOrder, status: str, response=None) -> None:
        per_set = order.profit / order.sets
        for leg, px in zip(order.basket.legs, order.limits):
            self.db.log_signal(mode="live" if self.live else "paper", market_id=leg.market_id,
                               exchange_id=leg.exchange_id, side=order.side, action="buy", price=px,
                               quantity=order.sets, edge=per_set, status=status,
                               response={"basket": order.basket.key, **({"r": response} if response else {})})

    def execute(self, order: ArbOrder) -> bool:
        b = order.basket
        desc = (f"{b.key} {order.side.upper()}×{len(b.legs)} sets={order.sets} cost={order.cost:.2f} "
                f"payout≥{order.payout:.0f} profit={order.profit:+.2f} limits={list(order.limits)}")
        if not self.live:
            log.info("PAPER ARB %s", desc)
            self._log(order, "paper")
            self.spent += order.cost
            return True
        legs = [{"exchangeId": l.exchange_id, "side": order.side, "quantity": order.sets, "price": px}
                for l, px in zip(b.legs, order.limits)]
        try:
            results = orders.place_multi_leg(self.client, legs, self.tid, ttl_seconds=self.cfg.order_ttl)
        except SigAPIError as e:
            log.error("arb rejected %s: %s", desc, e)
            self._log(order, f"error:{e.code}", {"message": e.message, "details": e.details})
            return False
        filled = [self._settle_leg(r) for r in results]
        cost = sum(float(r.get("totalCost") or 0) for r in results)
        self.spent += cost
        self.cash -= cost
        self._positions_at = -1e9  # re-read holdings next cycle
        log.info("LIVE ARB %s → filled %s", desc, filled)
        self._log(order, "sent", results)
        if max(filled) - min(filled) >= 1:
            self._open_repair(order, filled)
        return True

    def execute_exit(self, order: ExitOrder) -> bool:
        b = order.basket
        cost_per_set = self.held_cost.get(b.key, 0.0)
        realized = order.realized
        desc = (f"{b.key} SELL NO×{len(b.legs)} sets={order.sets} proceeds={order.proceeds:.2f} "
                f"(cost {order.sets * cost_per_set:.2f}, hold {order.sets * b.payout('no'):.0f}): "
                f"profit={realized if realized is not None else float('nan'):+.2f} vs holding {order.gain:+.2f} "
                f"limits={list(order.limits)}")
        per_set = (realized if realized is not None else order.gain) / order.sets
        mode = "live" if self.live else "paper"

        def log_legs(status, response=None):
            for leg, px in zip(b.legs, order.limits):
                self.db.log_signal(mode=mode, market_id=leg.market_id, exchange_id=leg.exchange_id, side="no",
                                   action="sell", price=px, quantity=order.sets, edge=per_set, status=status,
                                   response={"basket": b.key, **({"r": response} if response else {})})

        if not self.live:
            log.info("PAPER EXIT %s", desc)
            log_legs("paper")
            self.exits += 1
            return True
        legs = [{"exchangeId": l.exchange_id, "side": "no", "action": "sell", "quantity": order.sets, "price": px}
                for l, px in zip(b.legs, order.limits)]
        try:
            results = orders.place_multi_leg(self.client, legs, self.tid, ttl_seconds=self.cfg.order_ttl)
        except SigAPIError as e:
            log.error("exit rejected %s: %s", desc, e)
            log_legs(f"error:{e.code}", {"message": e.message, "details": e.details})
            return False
        sold = [self._settle_leg(r) for r in results]
        proceeds = sum(abs(float(r.get("totalCost") or 0)) for r in results)
        self.cash += proceeds
        self.spent = max(0.0, self.spent - cost_per_set * min(sold))
        self.exits += 1
        self._positions_at = -1e9  # re-read holdings next cycle
        log.info("LIVE EXIT %s → sold %s", desc, sold)
        log_legs("exit", results)
        if max(sold) - min(sold) >= 1:
            # Legs that sold more now hold less NO than the rest: buy that back to stay hedged,
            # paying at most what we sold it for plus repair_slippage.
            legs = [{"exchange_id": l.exchange_id, "title": l.title, "short": got - min(sold),
                     "cap": round(min(0.995, px + self.cfg.repair_slippage), 3)}
                    for l, px, got in zip(b.legs, order.limits, sold) if got - min(sold) >= 1]
            repair = {"side": "no", "legs": legs, "source": "exit"}
            log.warning("exit %s sold unevenly %s: repair %s", b.key, sold, legs)
            self.db.save_repair(b.key, repair)
            self.repairs[b.key] = repair
            self.work_repair(b.key)
        return True

    def _settle_leg(self, r: dict) -> float:
        """Cancel the leg if it is resting, then return how many shares it bought."""
        traded = abs(float(r.get("quantityTraded") or 0))
        oid = r.get("orderId")
        if not r.get("open") or oid is None:
            return traded
        try:
            orders.cancel(self.client, oid)
        except SigAPIError as e:
            log.warning("cancel %s: %s", oid, e)
        # Fills can land between placement and cancel, so count them from the fills list.
        # NO fills come back with negative quantities.
        try:
            fills = self.client.get(f"/orders/{oid}/fills").get("data", [])
            return max(traded, sum(abs(float(f.get("quantity") or 0)) for f in fills))
        except SigAPIError:
            return traded

    # ---- repairs ----

    def _open_repair(self, order: ArbOrder, filled: List[float]) -> None:
        """Save the shortfall. Each short leg may cost up to its planned price plus the planned
        profit per set (so completing it at the cap breaks even) plus repair_slippage."""
        b, target = order.basket, max(filled)
        profit_per_set = order.profit / order.sets
        legs = [{"exchange_id": leg.exchange_id, "title": leg.title, "short": target - got,
                 "cap": round(min(0.995, px + profit_per_set + self.cfg.repair_slippage), 3)}
                for leg, px, got in zip(b.legs, order.limits, filled) if target - got >= 1]
        repair = {"side": order.side, "legs": legs, "source": "bot"}
        log.warning("arb %s filled unevenly %s: repair %s", b.key, filled, legs)
        self.db.save_repair(b.key, repair)
        self.repairs[b.key] = repair
        self.work_repair(b.key)

    def work_repair(self, race: str) -> None:
        rep = self.repairs[race]
        legs = [l for l in rep["legs"] if l["short"] >= 1]
        if not legs:
            self.db.delete_repair(race)
            return
        if not self._can_read(len(legs)):
            return
        # No freshness check here: every leg's limit is at or under its cap, so a stale book can
        # only mean the order doesn't fill, never that it overpays.
        books, _ = self._books([l["exchange_id"] for l in legs])
        plan = []
        for l in legs:
            ladder = books[l["exchange_id"]].no_asks() if rep["side"] == "no" else books[l["exchange_id"]].asks
            qty, limit = 0.0, None
            for lvl in ladder:
                if lvl.price > l["cap"] + 1e-9 or qty >= l["short"]:
                    break
                qty += lvl.quantity
                limit = lvl.price
            take = int(min(qty, l["short"]))
            if take >= 1:
                plan.append((l, take, limit))
        if not plan:
            log.info("repair %s: nothing on the book at or below the caps %s", race,
                     [(l["title"], l["cap"]) for l in legs])
            return
        try:
            results = orders.place_multi_leg(
                self.client, [{"exchangeId": l["exchange_id"], "side": rep["side"], "quantity": take, "price": px}
                              for l, take, px in plan], self.tid, ttl_seconds=self.cfg.repair_ttl)
        except SigAPIError as e:
            log.error("repair %s rejected: %s", race, e)
            return
        for (l, take, px), r in zip(plan, results):
            got = self._settle_leg(r)
            l["short"] = max(0.0, l["short"] - got)
            self.db.log_signal(mode="live", market_id=None, exchange_id=l["exchange_id"], side=rep["side"],
                               action="buy", price=px, quantity=int(got), status="repair",
                               response={"basket": race, "r": r})
            log.info("repair %s: bought %d of %s at ≤%.3f, %.0f still short", race, got, l["title"], px, l["short"])
        if all(l["short"] < 1 for l in rep["legs"]):
            log.info("repair %s complete: basket hedged", race)
            self.db.delete_repair(race)
            del self.repairs[race]
        else:
            self.db.save_repair(race, rep)

    # ---- dashboard heartbeat ----

    def write_status(self) -> None:
        opps = []
        for b in self.baskets:
            qs = [self._quotes.get(l.exchange_id, (None, None)) for l in b.legs]
            bids, asks = [q[0] for q in qs], [q[1] for q in qs]
            opps.append({
                "race": b.key, "legs": "".join(l.label for l in b.legs), "title": b.legs[0].title,
                "sum_bid": sum(bids) if all(x is not None for x in bids) else None,
                "sum_ask": sum(asks) if all(x is not None for x in asks) else None,
                "frozen": b.key in self.repairs, "held": self.held.get(b.key, 0),
            })
        opps.sort(key=lambda o: -(o["sum_bid"] or 0))
        self.db.set_status({
            "mode": "live" if self.live else "paper", "started": self.started, "cycles": self.cycles,
            "trades": self.trades, "baskets": len(self.baskets), "cash": self.cash, "spent": self.spent,
            "max_capital": self.cfg.max_capital, "min_profit": self.cfg.min_profit,
            "poll_seconds": self.cfg.poll_seconds, "allow_yes": self.cfg.allow_yes,
            "frozen": sorted(self.repairs), "stale_skips": self.stale_skips, "last_error": self.last_error,
            "exits": self.exits, "exit_enabled": self.cfg.exit_enabled, "exit_only": self.cfg.exit_only,
            "quoting": self.quoter.status(self._quotes) if self.quoter else None,
            "directional": self.director.status() if self.director else None,
            "mm": self.mm.status(self._quotes) if self.mm else None,
            "feed": None if self.feed is None else {
                "healthy": self.feed.healthy, "watching": len(self.feed.market_ids),
                "subscribed": len(self.feed.subscribed), "hits": self.feed_hits, "misses": self.feed_misses,
                **{k: v for k, v in self.feed.feed.stats.items()
                   if k in ("batches", "gaps", "resyncs", "reconnects", "verifies", "verify_behind")}},
            "held": self._held_status(),
            "kill_switch": self.s.kill_switch.exists(), "opportunities": opps[:25],
        })

    def _held_status(self) -> List[Dict[str, Any]]:
        out = []
        for b in self.baskets:
            n = self.held.get(b.key)
            if not n:
                continue
            asks = [self._quotes.get(l.exchange_id, (None, None))[1] for l in b.legs]
            cost = self.held_cost.get(b.key)
            out.append({"race": b.key, "title": b.legs[0].title, "sets": n, "cost_per_set": cost,
                        "locked": n * (b.payout("no") - cost) if cost is not None else None,
                        "sum_ask": sum(asks) if all(a is not None for a in asks) else None,
                        # exits once Σ YES asks ≤ this (proceeds Σ(1 − ask) reach the bar)
                        "exit_at": round(len(b.legs) - self._exit_bar(b), 6)})
        return out

    # ---- main loop ----

    def _acquire_lock(self) -> None:
        """One bot per database: two live bots double their orders and split the read budget
        (seen live: both sold the same basket in the same second). The OS releases the lock if
        the process dies, so a crash never leaves it stuck."""
        path = self.s.db_path.with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(path, "a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.seek(0)
            other = self._lock_file.read().strip() or "?"
            raise SystemExit(f"Another bot is already running on {self.s.db_path} (pid {other}). "
                             f"Stop it first, e.g. `kill {other}`.")
        self._lock_file.seek(0)
        self._lock_file.truncate()
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

    def run(self, max_cycles: Optional[int] = None) -> None:
        self._acquire_lock()
        # Proof of life first: on a slow exchange the startup reads below take minutes.
        self._starting = True
        threading.Thread(target=self._alive_loop, daemon=True, name="alive").start()
        # The tournament has no engine relationships, and that read took 36 s on a slow night,
        # so startup no longer checks `arb.violations`.
        call_patiently(self.client, self.refresh, "loading markets")
        if self.quoter:
            call_patiently(self.client, self.quoter.startup, "clearing leftover quotes")
        if self.feed is not None:
            self.feed.start_in_thread()
        if self.fair is not None:
            self.fair.start()
        self._starting = False
        try:
            self._loop(max_cycles)
        finally:
            self._alive_stop.set()
            if self.quoter:
                self.quoter.cancel_all("bot stopping")
            if self.feed is not None:
                self.feed.stop()
            if self.fair is not None:
                self.fair.stop()

    def _alive_loop(self) -> None:
        """Every 10 s, independent of the trading loop: proof of life for the dashboard. On a slow
        exchange one cycle can take minutes, and the per-cycle heartbeat alone looks like a crash."""
        db = DB(self.s.db_path)  # own connection: this runs on another thread
        while not self._alive_stop.is_set():
            try:
                db.set_alive(self._cycle_started, self.cycles, "live" if self.live else "paper",
                             starting=self._starting)
            except Exception:
                log.debug("alive write failed", exc_info=True)
            self._alive_stop.wait(10)

    def _loop(self, max_cycles: Optional[int]) -> None:
        n = 0
        while max_cycles is None or n < max_cycles:
            t0 = time.monotonic()
            self._cycle_started = time.time()
            try:
                self.trades += self.step()
            except SigAPIError as e:
                log.error("cycle failed: %s", e)
                self.last_error = f"{time.strftime('%H:%M:%S')} {e}"
            except Exception as e:
                log.exception("cycle failed")
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}"
            n += 1
            self.cycles = n
            self._cycle_started = None
            try:
                self.write_status()
            except Exception:
                log.exception("status write failed")
            self._wait_until(t0 + self.cfg.poll_seconds)

    def _wait_until(self, deadline: float) -> None:
        """Sleep until the next cycle, but wake at once when the account channel pushes fills:
        a filled quote is hedged within about a second instead of at the next cycle."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not self._wake.wait(left):
                return
            self._wake.clear()
            if self.quoter is None:
                continue
            try:
                self.quoter.process_pushed(self._quotes)
            except SigAPIError as e:
                log.error("hedging pushed fills failed: %s", e)
                self.last_error = f"{time.strftime('%H:%M:%S')} {e}"
            except Exception as e:
                log.exception("hedging pushed fills failed")
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}"
