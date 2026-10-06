"""Market making around the fair value (`sigbot arb --mm`). Paper only for now; pricing and
inventory live in trading/mm.py.

Each cycle, with the bot's bulk quotes and last-trade prices:
  1. Fills (paper): a quote fills when a new trade prints at or through its price. Optimistic: it
     ignores queue position and assumes the whole quote fills; quotes step a tick inside the book
     so they'd usually be first in line.
  2. Every MM_RESELECT seconds, pick MM_MARKETS markets (one per race) where both quotes can sit
     at the top of the book at least MM_EDGE from a Kalshi-backed fair value, most spread first.
     Races the directional trader holds are skipped: a YES fill there would cancel against its NO
     and break its ledger. Dropped markets with inventory keep quoting until flat.
  3. Requote at most MM_WRITES_PER_CYCLE sides a cycle (paper counts them like live writes).
No fresh fair value or the kill switch: every quote is pulled; inventory is kept.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .trading import mm

if TYPE_CHECKING:
    from .arb_bot import ArbBot

log = logging.getLogger(__name__)
ACTIVITY_WINDOW = 3600.0  # seconds of trade history used to rank markets by how often they trade


class MarketMaker:
    def __init__(self, bot: "ArbBot"):
        self.bot = bot
        self.cfg = bot.s.mm
        self.mode = "paper"
        self.inv: Dict[str, mm.Inventory] = {}
        self.meta: Dict[str, Tuple[str, str]] = {}  # exchange → (race, party)
        for ex, st in bot.db.mm_states(self.mode).items():
            self.inv[ex] = mm.Inventory(**{k: st[k] for k in mm.Inventory().as_dict()})
            self.meta[ex] = (st["race"], st["party"])
        self.chosen: List[str] = []
        self.quotes: Dict[str, Dict[str, Optional[float]]] = {}  # exchange → {"bid", "ask"}
        self._last: Dict[str, Optional[float]] = {}
        self._trades: Dict[str, List[float]] = {}  # exchange → times its last price changed
        self._selected_at = -1e9
        self._writes = 0
        self.stats: Dict[str, float] = {"fills": 0, "filled_shares": 0}

    # ---- fair values ----

    def _fairs(self) -> Dict[str, Tuple[str, str, float]]:
        """exchange → (race, party, P(party wins)) from Kalshi-backed edges."""
        out: Dict[str, Tuple[str, str, float]] = {}
        legs = {b.key: {l.label: l.exchange_id for l in b.legs} for b in self.bot.baskets}
        for e in self.bot._edges:
            if "kalshi" not in e.fv.sources or e.race not in legs:
                continue
            ls = legs[e.race]
            if "D" in ls:
                out[ls["D"]] = (e.race, "D", e.fv.p_d)
            if "R" in ls:
                out[ls["R"]] = (e.race, "R", 1 - e.fv.p_d)
        return out

    # ---- one cycle ----

    def step(self, quotes: Dict[str, Tuple], lasts: Dict[str, Optional[float]]) -> None:
        self._writes = 0
        now = time.monotonic()
        self._fills(lasts, now)
        fresh = self.bot.fair is not None and self.bot.fair.fresh()
        if self.bot.s.kill_switch.exists() or not fresh:
            self.quotes.clear()  # pulled; inventory stays
            return
        fairs = self._fairs()
        if now - self._selected_at >= self.cfg.reselect:
            self._select(quotes, fairs, now)
        for ex in self._working():
            if ex not in fairs:
                self.quotes.pop(ex, None)
                continue
            race, party, fair = fairs[ex]
            self.meta[ex] = (race, party)
            bid, ask = quotes.get(ex, (None, None))
            inv = self.inv.setdefault(ex, mm.Inventory())
            b, a = mm.quote_prices(fair, bid, ask, inv.net, self.cfg.edge, self.cfg.max_inv, self.cfg.skew)
            if ex not in self.chosen:  # dropped: only the side that unwinds
                b = b if inv.net < 0 else None
                a = a if inv.net > 0 else None
            cur = self.quotes.get(ex, {"bid": None, "ask": None})
            new = dict(cur)
            for side, px in (("bid", b), ("ask", a)):
                if px != cur.get(side) and self._writes < self.cfg.writes_per_cycle:
                    new[side] = px
                    self._writes += 1 if px is not None else 0
            if new["bid"] is None and new["ask"] is None:
                self.quotes.pop(ex, None)
            else:
                self.quotes[ex] = new

    def _working(self) -> List[str]:
        """Chosen markets, plus dropped ones still holding inventory."""
        return self.chosen + [ex for ex, i in self.inv.items() if ex not in self.chosen and abs(i.net) >= 1]

    def _select(self, quotes, fairs, now: float) -> None:
        held = set(self.bot.db.dir_positions("live")) | set(self.bot.db.dir_positions("paper"))
        scored = []
        for ex, (race, party, fair) in fairs.items():
            if race in held or race in self.bot.repairs:
                continue
            bid, ask = quotes.get(ex, (None, None))
            c = mm.capture(fair, bid, ask, self.cfg.edge)
            if c <= 0:
                continue
            active = len([t for t in self._trades.get(ex, []) if now - t < ACTIVITY_WINDOW])
            scored.append((c * (1 + active), race, ex))
        scored.sort(reverse=True)
        chosen, races = [], set()
        for _, race, ex in scored:
            if race not in races and len(chosen) < self.cfg.markets:
                chosen.append(ex)
                races.add(race)
        self.chosen, self._selected_at = chosen, now

    def _fills(self, lasts: Dict[str, Optional[float]], now: float) -> None:
        for ex, last in lasts.items():
            prev = self._last.get(ex)
            self._last[ex] = last
            if last is None or prev is None or last == prev:
                continue
            self._trades.setdefault(ex, []).append(now)
            q = self.quotes.get(ex)
            if not q:
                continue
            if q.get("bid") is not None and last <= q["bid"] + 1e-9:
                self._fill(ex, "yes", q["bid"])
            elif q.get("ask") is not None and last >= q["ask"] - 1e-9:
                self._fill(ex, "no", 1 - q["ask"])

    def _fill(self, ex: str, side: str, price: float) -> None:
        inv = self.inv.setdefault(ex, mm.Inventory())
        room = self.cfg.max_inv - (inv.net if side == "yes" else -inv.net)
        qty = max(0.0, min(self.cfg.size, room))
        if qty < 1:
            return
        banked = inv.fill(side, qty, price)
        race, party = self.meta.get(ex, ("?", "?"))
        self.bot.db.save_mm_state(self.mode, ex, race, party, inv.as_dict())
        self.stats["fills"] += 1
        self.stats["filled_shares"] += qty
        self.bot.db.log_signal(mode=self.mode, market_id=None, exchange_id=ex, side=side, action="buy",
                               price=price, quantity=int(qty), edge=banked / qty if banked else None,
                               status="mm-fill", response={"basket": race, "mm": True, "party": party})
        log.info("PAPER MM FILL %s %s: bought %d %s at %.3f, banked %+.2f, net %+.0f",
                 race, party, qty, side.upper(), price, banked, inv.net)

    # ---- dashboard ----

    def status(self, quotes: Dict[str, Tuple]) -> Dict[str, Any]:
        fairs = self._fairs()
        rows = []
        for ex in dict.fromkeys(self._working() + [e for e, i in self.inv.items() if i.pairs or abs(i.net) >= 1]):
            inv = self.inv.get(ex, mm.Inventory())
            race, party = self.meta.get(ex, ("?", "?"))
            fair = fairs.get(ex, (None, None, None))[2]
            bid, ask = quotes.get(ex, (None, None))
            q = self.quotes.get(ex, {})
            rows.append({"race": race, "party": party, "fair": fair, "book_bid": bid, "book_ask": ask,
                         "our_bid": q.get("bid"), "our_ask": q.get("ask"), "net": inv.net, "pairs": inv.pairs,
                         "realized": inv.realized, "unrealized": inv.unrealized(fair) if fair is not None else None,
                         "chosen": ex in self.chosen})
        return {"mode": self.mode, "markets": len(self.chosen), "rows": rows,
                "realized": sum(i.realized for i in self.inv.values()),
                "unrealized": sum(r["unrealized"] or 0 for r in rows), **self.stats}
