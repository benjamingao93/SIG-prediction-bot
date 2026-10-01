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
The kill switch blocks every order, repairs included.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from .api import markets as mk
from .api import orders
from .api.client import SigAPIError, SigClient
from .api.models import OrderBook
from .data.db import DB
from .config import Settings
from .data.external.races import load_races
from .trading import arb
from .trading.arb import ArbOrder, Basket

log = logging.getLogger(__name__)


class ArbBot:
    def __init__(self, s: Settings, client: SigClient, db: DB, live: bool):
        self.s, self.client, self.db, self.live, self.cfg = s, client, db, live, s.arb
        self.tournament = mk.get_tournament(client, s.tournament_slug)
        self.tid = self.tournament["id"]
        self.cash = float(self.tournament.get("myBalance") or 0)
        self.baskets: List[Basket] = []
        self.spent = 0.0
        self.repairs: Dict[str, Dict[str, Any]] = {}
        self._last_trade: Dict[str, float] = {}
        self._last_refresh = 0.0
        self._quotes: Dict[str, tuple] = {}
        self.cycles = 0
        self.trades = 0
        self.stale_skips = 0
        self.last_error: Optional[str] = None
        self.started = time.time()

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

    def _can_read(self, n: int) -> bool:
        """Room for n reads right now, keeping 3 back for the next cycle's bulk prices."""
        return self.client.reads.available >= n + 3

    def _books(self, exchange_ids) -> Tuple[Dict[str, OrderBook], float]:
        """Fresh books, read in parallel so a slow exchange costs one round trip, not one per
        leg, and the monotonic time the first of them arrived (the age that matters)."""
        ids = list(exchange_ids)
        arrived: Dict[str, float] = {}

        def read(ex):
            book = mk.get_orderbook(self.client, ex, self.tid)
            arrived[ex] = time.monotonic()
            return book

        with ThreadPoolExecutor(max_workers=max(1, len(ids))) as pool:
            books = dict(zip(ids, pool.map(read, ids)))
        return books, min(arrived.values())

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
        quotes = arb.quotes_from_prices(mk.bulk_prices(self.client, ex_ids, self.tid))
        self._quotes = quotes
        traded = 0
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
            order = arb.size(b, side, books, self.cfg.min_profit, self.cfg.max_sets)
            if order is None:
                continue
            order = self._fit_budget(order, books)
            if order is None or self._stale(read_at, f"arb {b.key}"):
                continue
            if self.execute(order):
                traded += 1
        return traded

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
        return arb.size(order.basket, order.side, books, self.cfg.min_profit, sets)

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
        log.info("LIVE ARB %s → filled %s", desc, filled)
        self._log(order, "sent", results)
        if max(filled) - min(filled) >= 1:
            self._open_repair(order, filled)
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
                              for l, take, px in plan], self.tid, ttl_seconds=self.cfg.order_ttl)
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
                "frozen": b.key in self.repairs,
            })
        opps.sort(key=lambda o: -(o["sum_bid"] or 0))
        self.db.set_status({
            "mode": "live" if self.live else "paper", "started": self.started, "cycles": self.cycles,
            "trades": self.trades, "baskets": len(self.baskets), "cash": self.cash, "spent": self.spent,
            "max_capital": self.cfg.max_capital, "min_profit": self.cfg.min_profit,
            "poll_seconds": self.cfg.poll_seconds, "allow_yes": self.cfg.allow_yes,
            "frozen": sorted(self.repairs), "stale_skips": self.stale_skips, "last_error": self.last_error,
            "kill_switch": self.s.kill_switch.exists(), "opportunities": opps[:25],
        })

    # ---- main loop ----

    def run(self, max_cycles: Optional[int] = None) -> None:
        self.refresh()
        for v in arb.violations(self.client, self.tid):
            log.info("engine-reported violation: %s", v.get("reason"))
        n = 0
        while max_cycles is None or n < max_cycles:
            t0 = time.monotonic()
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
            try:
                self.write_status()
            except Exception:
                log.exception("status write failed")
            time.sleep(max(0.0, self.cfg.poll_seconds - (time.monotonic() - t0)))
