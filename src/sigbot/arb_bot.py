"""Arbitrage loop: screen every race basket each cycle, size against the books, execute atomically.

Per cycle: 3 reads of bulk prices cover all 237 markets. Only baskets that clear the bar at the
top of the book get their books fetched (one read per leg). A trade is one multi-leg order
(one write), so the budget stays far inside 100 reads / 30 writes per minute.

Live execution:
  1. POST /orders/multi-leg with every leg at the worst level walked: all placed or none.
  2. Cancel any leg left resting (the book moved since we read it), then count its fills.
  3. If legs filled unevenly, buy the shortfall on the short legs, at most ARB_REPAIR_SLIPPAGE
     worse than planned. If that fails too, the basket is frozen and logged for you to fix.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set

from .api import markets as mk
from .api import orders
from .api.client import SigAPIError, SigClient
from .config import ArbConfig, Settings
from .data.db import DB
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
        self.frozen: Set[str] = set()  # baskets left unbalanced: no more trades until you check
        self._last_trade: Dict[str, float] = {}
        self._last_refresh = 0.0

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

    # ---- one cycle ----

    def step(self) -> int:
        """Returns the number of baskets traded."""
        if time.monotonic() - self._last_refresh > 600:
            self.refresh()
        ex_ids = [l.exchange_id for b in self.baskets for l in b.legs]
        quotes = arb.quotes_from_prices(mk.bulk_prices(self.client, ex_ids, self.tid))
        now = time.monotonic()
        traded = 0
        for b in self.baskets:
            if b.key in self.frozen or now - self._last_trade.get(b.key, -1e9) < self.cfg.basket_cooldown:
                continue
            side = arb.screen(b, quotes, self.cfg.min_profit, self.cfg.allow_yes)
            if side is None:
                continue
            books = {l.exchange_id: mk.get_orderbook(self.client, l.exchange_id, self.tid) for l in b.legs}
            order = arb.size(b, side, books, self.cfg.min_profit, self.cfg.max_sets)
            if order is None:
                continue
            order = self._fit_budget(order, books)
            if order is None:
                continue
            self._last_trade[b.key] = time.monotonic()
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
            self._repair(order, filled)
        return True

    def _settle_leg(self, r: dict) -> float:
        """Cancel the leg if it is resting, then return how many shares it bought."""
        traded = float(r.get("quantityTraded") or 0)
        oid = r.get("orderId")
        if not r.get("open") or oid is None:
            return traded
        try:
            orders.cancel(self.client, oid)
        except SigAPIError as e:
            log.warning("cancel %s: %s", oid, e)
        # Fills can land between placement and cancel, so count them from the fills list.
        try:
            fills = self.client.get(f"/orders/{oid}/fills").get("data", [])
            return max(traded, sum(float(f.get("quantity") or 0) for f in fills))
        except SigAPIError:
            return traded

    def _repair(self, order: ArbOrder, filled: List[float]) -> None:
        """Buy the shortfall on legs that filled less, within repair_slippage of the plan."""
        b, target = order.basket, max(filled)
        legs = []
        for leg, px, got in zip(b.legs, order.limits, filled):
            short = int(round(target - got))
            if short > 0:
                legs.append({"exchangeId": leg.exchange_id, "side": order.side, "quantity": short,
                             "price": min(0.995, px + self.cfg.repair_slippage)})
        log.warning("arb %s unbalanced %s: repairing %s", b.key, filled, legs)
        try:
            results = orders.place_multi_leg(self.client, legs, self.tid, ttl_seconds=self.cfg.order_ttl)
            got = [self._settle_leg(r) for r in results]
        except SigAPIError as e:
            log.error("repair failed for %s: %s", b.key, e)
            got = [0.0]
        if any(g < l["quantity"] for g, l in zip(got, legs)):
            self.frozen.add(b.key)
            log.error("arb %s still unbalanced after repair: frozen. Check positions and fix by hand.", b.key)

    # ---- main loop ----

    def run(self, max_cycles: Optional[int] = None) -> None:
        self.refresh()
        for v in arb.violations(self.client, self.tid):
            log.info("engine-reported violation: %s", v.get("reason"))
        n = 0
        while max_cycles is None or n < max_cycles:
            t0 = time.monotonic()
            try:
                self.step()
            except SigAPIError as e:
                log.error("cycle failed: %s", e)
            except Exception:
                log.exception("cycle failed")
            n += 1
            time.sleep(max(0.0, self.cfg.poll_seconds - (time.monotonic() - t0)))
