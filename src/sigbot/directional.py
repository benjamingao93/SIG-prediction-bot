"""Phase 1 directional trader (`sigbot arb --directional`): short holds against the fair value.

Each cycle, using the edges the FairValueService computed from this cycle's SIG prices:

Exits first (they free capital):
  - sell half once SIG's sell price (the NO bid, 1 − YES ask) has closed DIR_TAKE_HALF_AT of the gap
    between the entry price and today's fair value, at a profit;
  - sell the rest once that price is within DIR_EXIT_BAND of fair value, at a profit;
  - cut the position if the fair value falls DIR_STOP below the entry price (the view broke).
  With no fresh fair value (Kalshi stale), positions are left alone.
Entries, best return on capital (edge ÷ price) first:
  - only views that clear the bar (and, with DIR_REQUIRE_AGREEMENT, where every source agrees),
    one position per race;
  - bought as NO on the other party's market, so it never cancels against basket holdings;
  - sized by the smallest of: the book's depth while each level still clears the bar (with the
    ARB_DEPTH_FRACTION cushion), ¼-Kelly on the budget, the per-race cap, the budget left, and the
    net-direction cap; nothing new once losses reach DIR_LOSS_STOP.
Positions live in their own ledger (dir_positions), so the arb bot's basket detection, exits and
repairs never mistake them for uneven baskets. Paper mode simulates fills at the book's prices.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .api import orders
from .api.client import SigAPIError
from .models.fairvalue import Edge
from .trading import arb
from .trading.sizing import kelly_shares
from datetime import datetime, timezone


def _epoch(iso: Optional[str]) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else time.time()
    except ValueError:
        return time.time()

if TYPE_CHECKING:
    from .arb_bot import ArbBot

log = logging.getLogger(__name__)


class Director:
    def __init__(self, bot: "ArbBot"):
        self.bot = bot
        self.cfg = bot.s.dir
        self.live = bot.live and not self.cfg.paper  # --directional-paper: simulate inside a live bot
        self.mode = "live" if self.live else "paper"
        self.budget = self.cfg.live_budget if self.live else self.cfg.budget
        self.max_race = self.cfg.live_max_race if self.live else self.cfg.max_race
        self._writes = 0
        self.stats: Dict[str, float] = {"entries": 0, "exits": 0}

    # ---- state ----

    def positions(self) -> Dict[str, Dict[str, Any]]:
        return self.bot.db.dir_positions(self.mode)

    def sell_price(self, exchange_id: str) -> Optional[float]:
        """What selling NO pays right now: 1 − the market's YES ask."""
        ask = self.bot._quotes.get(exchange_id, (None, None))[1]
        return None if ask is None else round(1 - ask, 6)

    def _edge(self, race: str, view: str) -> Optional[Edge]:
        for e in self.bot._edges:
            if e.race == race and e.view == view and "kalshi" in e.fv.sources:
                return e  # only trust Kalshi-backed fair values for exits too
        return None

    def _fair(self, race: str, view: str) -> Optional[float]:
        e = self._edge(race, view)
        return e.fair if e else None

    def unrealized(self, positions: Dict[str, Dict[str, Any]]) -> float:
        total = 0.0
        for p in positions.values():
            px = self.sell_price(p["exchange_id"])
            if px is not None:
                total += (px - p["entry_price"]) * p["qty"]
        return total

    def net_direction(self, positions: Dict[str, Dict[str, Any]]) -> float:
        return sum(p["cost"] if p["view"] == "D" else -p["cost"] for p in positions.values())

    # ---- one cycle ----

    def step(self) -> None:
        self._writes = 0
        if self.bot.s.kill_switch.exists():
            return
        fresh = self.bot.fair is not None and self.bot.fair.fresh()
        held = self.positions()
        if fresh:
            for race, p in list(held.items()):
                if self._writes >= self.cfg.writes_per_cycle:
                    break
                self._manage(p)
        if not fresh or self.bot.cfg.exit_only:
            return
        held = self.positions()
        if self.bot.db.dir_realized(self.mode) + self.unrealized(held) <= -self.cfg.loss_stop:
            log.warning("directional: loss stop reached, no new entries")
            return
        # New races, and top-ups of a race already held the same way (up to the race cap). Never
        # the opposite view in a race already held.
        cands = [e for e in self.bot._edges if e.tradeable and (e.agree or not self.cfg.require_agreement)
                 and "kalshi" in e.fv.sources and e.race not in self.bot.repairs
                 and (e.race not in held or (held[e.race]["view"] == e.view
                                             and held[e.race]["exchange_id"] == e.buy_exchange
                                             and self.max_race - held[e.race]["cost"] >= self.cfg.min_order))]
        cands.sort(key=lambda e: -(e.edge / e.price))
        if cands and self.room(held) < max(self.cfg.min_order, min(self.max_race, 1000.0)):
            if self._recycle(cands[0], held):
                cands = cands[:1]  # the freed cash is for the race it was freed for, nothing else
            held = self.positions()
        if self.live and self.bot.cash - self.cfg.cash_reserve < max(self.cfg.min_order, 1000.0):
            cands = cands[:1]  # nearly out of cash: no small top-ups that recycling would undo
        for e in cands:
            if self._writes >= self.cfg.writes_per_cycle or not self.bot._can_read(1):
                break
            self._enter(e, held)
            held = self.positions()

    def room(self, held: Dict[str, Dict[str, Any]]) -> float:
        """What a new entry may spend: budget left and, live, cash above the reserve."""
        left = self.budget - sum(p["cost"] for p in held.values())
        if self.live:
            left = min(left, self.bot.cash - self.cfg.cash_reserve)
        return max(0.0, left)

    @staticmethod
    def _spare_return(e: Edge, price: float) -> float:
        """Return on capital beyond the race's bar: (fair − required − price) ÷ price. The bar grows
        with the fair value's uncertainty, so a close race's gap counts for less than a favourite's."""
        return (e.fair - e.required - price) / price

    def remaining_return(self, p: Dict[str, Any]) -> Optional[float]:
        """What holding a position still earns beyond its bar, per SUSQie it would sell for now. The
        sell price is the bid, so the spread paid to get out is already in the comparison."""
        e, px = self._edge(p["race"], p["view"]), self.sell_price(p["exchange_id"])
        if e is None or not px:
            return None
        return self._spare_return(e, px)

    def _recycle(self, best: Edge, held: Dict[str, Dict[str, Any]]) -> bool:
        """Budget or cash is full: sell part of the position with the least return left, if the best
        new gap beats it by recycle_margin. Only as many shares as the new buy can use (its book depth,
        race cap and Kelly size, less the cash already free), so nothing is sold that would just be
        bought back. Positions bought into in the last 30 minutes are left alone. True if it sold."""
        plan = self._plan(best, held, ignore_room=True)
        if plan is None:
            return False
        qty, _, cost, _ = plan
        need = cost - self.room(held)
        if need < self.cfg.min_order:
            return False
        best_r = self._spare_return(best, cost / qty)  # at the average price the buy would pay
        now = time.time()
        scored = []
        for p in held.values():
            if p["race"] == best.race:
                continue  # never sell a race just to buy it back
            r = self.remaining_return(p)
            age = now - _epoch(p.get("last_buy") or p.get("opened"))  # protected 30 min after any buy
            if r is not None and age >= 1800:
                scored.append((r, p))
        if not scored:
            return False
        weakest_r, weakest = min(scored, key=lambda x: x[0])
        if best_r - weakest_r < self.cfg.recycle_margin:
            return False
        px = self.sell_price(weakest["exchange_id"])
        shares = min(weakest["qty"], math.ceil(need / px))
        return self._sell(weakest, shares, px,
                          f"recycle {shares} into {best.race} ({best_r:.1%} vs {weakest_r:.1%} left beyond the bar)")

    # ---- exits ----

    def _manage(self, p: Dict[str, Any]) -> None:
        fair = self._fair(p["race"], p["view"])
        px = self.sell_price(p["exchange_id"])
        if fair is None or px is None:
            return
        entry = p["entry_price"]
        if fair < entry - self.cfg.stop:
            self._sell(p, p["qty"], px, f"stop: fair {fair:.3f} fell below entry {entry:.3f}")
        elif px > entry and px >= fair - self.cfg.exit_band:
            self._sell(p, p["qty"], px, f"take profit: {px:.3f} reached fair {fair:.3f}")
        elif (not p["halved"] and px > entry and fair > entry
              and px >= entry + self.cfg.take_half_at * (fair - entry) and p["qty"] >= 2):
            self._sell(p, math.floor(p["qty"] / 2), px, f"take half: {px:.3f} closed half the gap to {fair:.3f}",
                       halved=True)

    def _sell(self, p: Dict[str, Any], qty: float, px: float, why: str, halved: bool = False) -> bool:
        qty = int(qty)
        if qty < 1:
            return False
        if self.live:
            self._writes += 1
            try:
                r = orders.place_limit(self.bot.client, p["exchange_id"], "no", "sell", qty, px, self.bot.tid,
                                       ttl_seconds=self.bot.cfg.order_ttl)
            except SigAPIError as e:
                log.error("directional sell %s rejected: %s", p["race"], e)
                return False
            got = self.bot._settle_leg(r)
            proceeds = abs(float(r.get("totalCost") or 0)) or got * px
        else:
            got, proceeds = qty, qty * px
        if got < 1:
            return False
        if self.live:
            self.bot.cash += proceeds  # spendable this cycle, as buys already subtract their cost
        cost_out = p["entry_price"] * got
        realized = proceeds - cost_out
        p = {**p, "qty": p["qty"] - got, "cost": p["cost"] - cost_out, "realized": p["realized"] + realized,
             # A half sale only counts once it fully fills; a partial fill tries again next cycle.
             "halved": 1 if (halved and got >= qty) or p["halved"] else 0}
        self.bot.db.add_dir_realized(self.mode, realized)
        if p["qty"] < 1:
            self.bot.db.close_dir_position(self.mode, p["race"])
        else:
            self.bot.db.save_dir_position(p)
        self.stats["exits"] += 1
        self._log(p, "sell", got, px, realized / got)
        log.info("%s DIRECTIONAL SELL %s %s: %d NO at %.3f, %+.2f (%s)", self.mode.upper(), p["race"], p["view"],
                 got, px, realized, why)
        return True

    # ---- entries ----

    def _plan(self, e: Edge, held: Dict[str, Dict[str, Any]], ignore_room: bool = False):
        """(shares, worst price, cost, read time) a buy of this edge would make now, or None.
        ignore_room: size as if budget and cash were no limit (what recycling would need to free)."""
        books, read_at = self.bot._books([e.buy_exchange])
        ladder = books[e.buy_exchange].no_asks()
        walk = arb._walk([ladder], lambda prices: e.fair - prices[0], e.required, 10 ** 9,
                         self.cfg.depth_fraction)
        if not walk:
            return None
        depth_qty, (limit,), total = walk
        have = held.get(e.race)
        room = self.max_race - (have["cost"] if have else 0.0)
        if not ignore_room:
            room = min(room, self.room(held))
        net = self.net_direction(held)
        sign = 1 if e.view == "D" else -1
        room = min(room, max(0.0, self.cfg.max_net - sign * net))  # how far this side may still go
        kelly = kelly_shares(e.fair, total / depth_qty, self.budget, self.cfg.kelly_fraction) - (have["qty"] if have else 0)
        qty = min(depth_qty, kelly,
                  int(room // limit) if limit > 0 else 0)
        if qty < 1 or qty * limit < self.cfg.min_order:
            return None
        # Re-walk for exactly qty shares: the cost and the worst level we actually need.
        _, (limit,), cost = arb._walk([ladder], lambda prices: e.fair - prices[0], e.required, qty,
                                      self.cfg.depth_fraction)
        return qty, limit, cost, read_at

    def _enter(self, e: Edge, held: Dict[str, Dict[str, Any]]) -> None:
        plan = self._plan(e, held)
        if plan is None:
            return
        qty, limit, cost, read_at = plan
        if self.bot._stale(read_at, f"directional {e.race}"):
            return
        have = held.get(e.race)
        if self.live:
            self._writes += 1
            try:
                r = orders.place_limit(self.bot.client, e.buy_exchange, "no", "buy", qty, limit, self.bot.tid,
                                       ttl_seconds=self.bot.cfg.order_ttl)
            except SigAPIError as err:
                log.error("directional buy %s rejected: %s", e.race, err)
                return
            got = self.bot._settle_leg(r)
            cost = abs(float(r.get("totalCost") or 0)) or got * limit
        else:
            got = qty
        if got < 1:
            return
        market = next((l for b in self.bot.baskets if b.key == e.race for l in b.legs
                       if l.exchange_id == e.buy_exchange), None)
        bought_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if have:  # top-up: one position per race, entry price averaged over all buys
            p = {**have, "qty": have["qty"] + got, "cost": have["cost"] + cost,
                 "entry_price": (have["cost"] + cost) / (have["qty"] + got), "fair_entry": e.fair,
                 "last_buy": bought_at}
        else:
            p = {"mode": self.mode, "race": e.race, "view": e.view, "exchange_id": e.buy_exchange,
                 "market_id": market.market_id if market else None, "title": market.title if market else "",
                 "qty": got, "cost": cost, "entry_price": cost / got, "fair_entry": e.fair, "halved": 0,
                 "realized": 0.0, "last_buy": bought_at}
        self.bot.db.save_dir_position(p)
        self.bot.cash -= cost if self.live else 0
        self.stats["entries"] += 1
        self._log(p, "buy", got, cost / got, e.fair - cost / got)
        log.info("%s DIRECTIONAL BUY %s (%s wins): %d NO at %.3f avg, fair %.3f, edge %+.3f/share",
                 self.mode.upper(), e.race, e.view, got, cost / got, e.fair, e.fair - cost / got)

    # ---- logging & dashboard ----

    def _log(self, p: Dict[str, Any], action: str, qty: float, price: float, edge: float) -> None:
        self.bot.db.log_signal(mode=self.mode, market_id=p.get("market_id"), exchange_id=p["exchange_id"],
                               side="no", action=action, price=price, quantity=int(qty), edge=edge,
                               status=f"dir-{action}", response={"basket": p["race"], "directional": True})

    def status(self) -> Dict[str, Any]:
        held = self.positions()
        rows = []
        for p in held.values():
            px = self.sell_price(p["exchange_id"])
            rows.append({"race": p["race"], "view": p["view"], "qty": p["qty"], "entry": p["entry_price"],
                         "price": px, "fair": self._fair(p["race"], p["view"]), "halved": bool(p["halved"]),
                         "unrealized": None if px is None else (px - p["entry_price"]) * p["qty"], "opened": p["opened"]})
        return {"mode": self.mode, "budget": self.budget, "max_race": self.max_race,
                "used": sum(p["cost"] for p in held.values()), "net": self.net_direction(held),
                "realized": self.bot.db.dir_realized(self.mode), "unrealized": self.unrealized(held),
                "positions": rows, **self.stats}

    def ledger_qty(self) -> Dict[str, float]:
        """exchange → NO shares the directional trader holds (live), for basket detection."""
        out: Dict[str, float] = {}
        for p in self.bot.db.dir_positions("live").values():
            out[p["exchange_id"]] = out.get(p["exchange_id"], 0.0) + p["qty"]
        return out

    def ledger_cost(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for p in self.bot.db.dir_positions("live").values():
            out[p["exchange_id"]] = out.get(p["exchange_id"], 0.0) + p["cost"]
        return out
