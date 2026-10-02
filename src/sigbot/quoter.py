"""Passive quoting for the arb bot (`sigbot arb --quote`). Pricing lives in trading/quoting.py.

Each cycle, after the bulk quotes:
  1. Fills. Live: one read of the tournament fills list, summed per quote order id, compared
     with what we've already hedged. Paper: a quote "fills" when a new trade prints at or through
     its price (optimistic: ignores queue position).
  2. Hedge every new fill by adding it to the race's repair (the bot's existing machinery buys
     the other legs' NO up to break-even + ARB_REPAIR_SLIPPAGE, and keeps trying every cycle).
     Paper: hedge at the current top of book and record the simulated profit.
  3. Maintain. Cancel a quote whose race has a repair pending, or whose fill would no longer
     make `edge` at current prices (immediately); reprice one that could move up a tick once
     it has rested ARB_QUOTE_MIN_LIFE, or that is about to expire.
  4. Place quotes in free slots: best-placed races first, sized by the hedge legs' depth.
Writes per cycle are capped so repairs and trades always have room. The kill switch and bot exit
cancel every quote; startup cancels any left over from a previous run (and hedges their fills).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .api import orders
from .api.client import SigAPIError
from .api.orders import TICK
from .trading import quoting
from .trading.arb import Basket

if TYPE_CHECKING:
    from .arb_bot import ArbBot

log = logging.getLogger(__name__)
RESELECT_SECONDS = 300.0
POLL_SECONDS = 30.0  # fills-list safety read when the account channel is trusted


def _closing_key(q: Dict[str, Any]) -> str:
    return f"{q['race']}@{q.get('order_id')}"


class Quoter:
    def __init__(self, bot: "ArbBot"):
        self.bot = bot
        self.cfg = bot.cfg
        self.active: Dict[str, Dict[str, Any]] = {}  # race → quote
        self.closing: List[Dict[str, Any]] = []  # cancelled live quotes whose last fills we still owe a check
        self.stats: Dict[str, float] = {"placed": 0, "cancelled": 0, "fills": 0, "filled_sets": 0, "paper_pnl": 0.0}
        self._writes = 0
        self._last_select = 0.0
        self._last_poll = 0.0
        self._poll_due = False

    # ---- helpers ----

    def _basket(self, race: str) -> Optional[Basket]:
        return next((b for b in self.bot.baskets if b.key == race), None)

    def _write_ok(self) -> bool:
        return self._writes < self.cfg.quote_writes_per_cycle

    def _log(self, q: Dict[str, Any], status: str, qty: float, edge: Optional[float] = None, response=None) -> None:
        self.bot.db.log_signal(mode="live" if self.bot.live else "paper", market_id=q.get("market_id"),
                               exchange_id=q["exchange_id"], side="no", action="buy", price=q["price"],
                               quantity=int(qty), edge=edge, status=status,
                               response={"basket": q["race"], "quote": True, **({"r": response} if response else {})})

    # ---- lifecycle ----

    def startup(self) -> None:
        """Cancel quotes a previous run left resting, and hedge anything they filled."""
        for key, q in self.bot.db.get_quotes().items():
            race = q["race"]
            if self.bot.live and q.get("order_id"):
                if not self._try_cancel(q):
                    # Still possibly resting: keep retrying the cancel and watching its fills.
                    q["cancel_ok"] = False
                    self.closing.append(q)
                    continue
                try:
                    fills = self.bot.client.get(f"/orders/{q['order_id']}/fills").get("data", [])
                    got = sum(abs(float(f.get("quantity") or 0)) for f in fills)
                    if got - q["filled"] >= 1:
                        self._on_fill(q, got - q["filled"], quotes={})
                except SigAPIError as e:
                    log.error("couldn't check fills of leftover quote %s on %s: %s — check positions",
                              q["order_id"], race, e)
            self.bot.db.delete_quote(key)

    def cancel_all(self, reason: str) -> None:
        for race in list(self.active):
            self._cancel(race, reason, force=True)

    def step(self, quotes: Dict[str, Tuple], lasts: Dict[str, Optional[float]]) -> None:
        self._writes = 0
        if self.bot.s.kill_switch.exists():
            if self.active:
                log.warning("kill switch present: cancelling %d quotes", len(self.active))
                self.cancel_all("kill switch")
            return
        if self.bot.live:
            self._detect_live(quotes)
        else:
            self._detect_paper(quotes, lasts)
        self._maintain(quotes)
        self._fill_slots(quotes, lasts)

    # ---- 1-2: fills and hedges ----

    def process_pushed(self, quotes) -> None:
        """Called the moment the account channel pushes fills: if any are ours, read the fills
        list and hedge now instead of at the next cycle."""
        feed = self.bot.feed
        if feed is None or not self.bot.live:
            return
        ours = {str(q.get("order_id")) for q in list(self.active.values()) + self.closing if q.get("order_id")}
        if any(str(f.get("orderId")) in ours for f in feed.pop_fills()):
            self._poll_due = True
            self._detect_live(quotes)

    def _detect_live(self, quotes) -> None:
        """Hedge new fills on our quotes, counted from the authoritative fills list. Read it every
        cycle unless the account channel is trusted, then only when it pushes one of our fills,
        while cancels are unconfirmed, or every POLL_SECONDS as a safety net."""
        self._retry_cancels()
        watched = list(self.active.values()) + self.closing
        feed = self.bot.feed
        if feed is not None:
            ours = {str(q.get("order_id")) for q in watched if q.get("order_id")}
            if any(str(f.get("orderId")) in ours for f in feed.pop_fills()):
                self._poll_due = True
        trusted = feed is not None and feed.user_trusted()
        due = (not trusted or self._poll_due or self.closing
               or time.monotonic() - self._last_poll > POLL_SECONDS)
        if not watched or not due or not self.bot._can_read(1):
            return
        try:
            fills = self.bot.client.get(f"/tournaments/{self.bot.s.tournament_slug}/portfolio/fills",
                                        limit=100).get("data", [])
        except SigAPIError as e:
            log.warning("quote fill check failed: %s", e)
            return
        self._last_poll, self._poll_due = time.monotonic(), False
        if feed is not None:
            feed.user_need_poll = False  # caught up with anything the channel may have missed
        by_order: Dict[str, float] = {}
        for f in fills:
            k = str(f.get("orderId"))
            by_order[k] = by_order.get(k, 0.0) + abs(float(f.get("quantity") or 0))
        for q in watched:
            got = by_order.get(str(q.get("order_id")), 0.0)
            if got - q["filled"] >= 1:
                self._on_fill(q, got - q["filled"], quotes)
        # A cancelled quote is done once its cancel is confirmed and a fills read came after it.
        for q in [q for q in self.closing if q.get("cancel_ok")]:
            self.closing.remove(q)
            self.bot.db.delete_quote(_closing_key(q))
        for race, q in list(self.active.items()):
            if q["size"] - q["filled"] < 1:  # fully filled: the order is gone
                del self.active[race]
                self.bot.db.delete_quote(race)

    def _try_cancel(self, q: Dict[str, Any]) -> bool:
        """True once the exchange confirms the order is no longer resting."""
        self._writes += 1
        try:
            orders.cancel(self.bot.client, q["order_id"])
            return True
        except SigAPIError as e:
            if e.status in (404, 409) or e.code in ("NOT_FOUND", "CONFLICT"):
                return True  # already filled, expired or cancelled
            log.warning("cancel of quote %s on %s not confirmed (%s): retrying next cycle",
                        q["order_id"], q["race"], e)
            return False

    def _retry_cancels(self) -> None:
        for q in self.closing:
            if not q.get("cancel_ok"):
                q["cancel_ok"] = self._try_cancel(q)
                self.bot.db.save_quote(_closing_key(q), q)

    def _detect_paper(self, quotes, lasts) -> None:
        for race, q in list(self.active.items()):
            now = lasts.get(q["exchange_id"])
            if quoting.would_fill(q["price"], q.get("paper_last"), now):
                self._on_fill(q, q["size"] - q["filled"], quotes)
                del self.active[race]
                self.bot.db.delete_quote(race)
            else:
                q["paper_last"] = now

    def _on_fill(self, q: Dict[str, Any], qty: float, quotes) -> None:
        q["filled"] += qty
        self.stats["fills"] += 1
        self.stats["filled_sets"] += qty
        legs = [{"exchange_id": ex, "title": t, "short": qty, "cap": q["caps"][ex]} for ex, t in q["hedge"]]
        if not self.bot.live:
            # Hedge at the current top of book: payout − our price − Σ NO asks of the other legs.
            asks = [1 - (quotes.get(ex, (None, None))[0] or 0.0) for ex, _ in q["hedge"]]
            pnl = qty * (q["payout"] - q["price"] - sum(asks))
            self.stats["paper_pnl"] += pnl
            log.info("PAPER QUOTE FILL %s: %d NO on %s at %.3f, hedged now at %s → %+.2f",
                     q["race"], qty, q["title"], q["price"], [round(a, 3) for a in asks], pnl)
            self._log(q, "paper-quote-fill", qty, edge=pnl / qty)
            return
        log.warning("QUOTE FILL %s: %d NO on %s at %.3f → hedging %s", q["race"], qty, q["title"], q["price"], legs)
        self._log(q, "quote-fill", qty, edge=q["edge"])
        if q["race"] in self.active:
            self.bot.db.save_quote(q["race"], q)
        rep = self.bot.db.add_to_repair(q["race"], "no", legs, "quote")
        self.bot.repairs[q["race"]] = rep
        self.bot.work_repair(q["race"])

    # ---- 3: maintenance ----

    def _cancel(self, race: str, reason: str, force: bool = False) -> bool:
        q = self.active.get(race)
        if q is None:
            return True
        if not force and not self._write_ok():
            return False
        if self.bot.live and q.get("order_id"):
            # Keep watching it (and keep it on disk for a restart) until the cancel is confirmed
            # and a fills read has come after it.
            q["cancel_ok"] = self._try_cancel(q)
            self.closing.append(q)
            self.bot.db.save_quote(_closing_key(q), q)
        del self.active[race]
        self.bot.db.delete_quote(race)
        self.stats["cancelled"] += 1
        log.info("quote %s cancelled (%s)", race, reason)
        return True

    def _maintain(self, quotes) -> None:
        now = time.time()
        for race, q in list(self.active.items()):
            if race in self.bot.repairs:
                self._cancel(race, "hedge pending", force=True)
                continue
            b = self._basket(race)
            spec = quoting.quote_price(b, q["leg"], quotes, self.cfg.quote_edge) if b else None
            if spec is None or spec.price < q["price"] - 1e-9:
                # A fill now would make less than `edge` (or lose): pull it at once.
                self._cancel(race, "edge gone", force=True)
            elif ((spec.price >= q["price"] + TICK - 1e-9 and now - q["placed"] >= self.cfg.quote_min_life)
                  or now > q["expires"] - 10):
                self._cancel(race, "reprice")

    # ---- 4: placement ----

    def _fill_slots(self, quotes, lasts) -> None:
        exclude = set(self.active) | set(self.bot.repairs)
        if time.monotonic() - self._last_select > RESELECT_SECONDS:
            self._last_select = time.monotonic()
            keep = {s.basket.key for s in quoting.select_quotes(self.bot.baskets, quotes, 2 * self.cfg.quote_races,
                                                                self.cfg.quote_edge)}
            for race in [r for r in self.active if r not in keep]:
                self._cancel(race, "better races available")
        free = self.cfg.quote_races - len(self.active)
        if free <= 0:
            return
        for spec in quoting.select_quotes(self.bot.baskets, quotes, free, self.cfg.quote_edge, exclude=exclude):
            if not self._write_ok():
                break
            self._place(spec, quotes, lasts)

    def _place(self, spec: quoting.QuoteSpec, quotes, lasts) -> None:
        hedge_ex = [ex for ex, _ in spec.hedge_asks]
        if not self.bot._can_read(len(hedge_ex)):
            return
        books, _ = self.bot._books(hedge_ex)
        r = quoting.reprice(spec, books, quotes, self.cfg.quote_edge, self.cfg.quote_size)
        if r is None:
            return
        spec, size = r
        if size * spec.price > self.bot.cash:
            return
        b, leg = spec.basket, spec.basket.legs[spec.leg]
        titles = {l.exchange_id: l.title for l in b.legs}
        now = time.time()
        q = {"race": b.key, "leg": spec.leg, "exchange_id": leg.exchange_id, "market_id": leg.market_id,
             "title": leg.title, "price": spec.price, "size": size, "filled": 0.0, "edge": round(spec.edge(), 6),
             "payout": b.payout("no"), "hedge": [[ex, titles[ex]] for ex in hedge_ex],
             "caps": quoting.hedge_caps(spec, self.cfg.quote_edge, self.cfg.repair_slippage),
             "placed": now, "expires": now + self.cfg.quote_ttl, "order_id": None,
             "paper_last": lasts.get(leg.exchange_id)}
        if self.bot.live:
            self._writes += 1
            try:
                resp = orders.place_limit(self.bot.client, leg.exchange_id, "no", "buy", size, spec.price,
                                          self.bot.tid, ttl_seconds=self.cfg.quote_ttl)
            except SigAPIError as e:
                log.error("quote rejected on %s: %s", b.key, e)
                self._log(q, f"error:{e.code}", size, response={"message": e.message})
                return
            q["order_id"] = resp.get("orderId")
        self.active[b.key] = q
        self.bot.db.save_quote(b.key, q)
        self.stats["placed"] += 1
        self._log(q, "quote", size, edge=q["edge"])
        log.info("%s QUOTE %s: bid %d NO on %s at %.3f (inside %.3f), hedge %s → +%.3f/set",
                 "LIVE" if self.bot.live else "PAPER", b.key, size, leg.title, spec.price, spec.score,
                 [(t, a) for (_, t), (_, a) in zip(q["hedge"], spec.hedge_asks)], q["edge"])

    # ---- dashboard ----

    def status(self, quotes) -> Dict[str, Any]:
        now = time.time()
        rows = []
        for q in self.active.values():
            asks = [1 - (quotes.get(ex, (None, None))[0] or 0.0) for ex, _ in q["hedge"]]
            rows.append({"race": q["race"], "title": q["title"], "price": q["price"], "size": q["size"],
                         "filled": q["filled"], "age": round(now - q["placed"]),
                         "edge_now": round(q["payout"] - q["price"] - sum(asks), 4)})
        return {"active": rows, **self.stats}
