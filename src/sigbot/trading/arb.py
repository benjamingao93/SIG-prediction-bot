"""Riskless baskets across mutually exclusive markets.

Each race is listed as separate binary markets, one per party ("Will the Democratic Party win
the Texas Senate?", "... Republican ..."). At most one of them can resolve YES, so for a race
with k markets:

  NO basket:  buy NO on all k. At least k−1 legs pay 1, whoever wins (an unlisted winner pays
              all k). Locked profit per set = (k−1) − Σ(1 − bid_i) = Σ bid_i − 1.
              Riskless whenever the YES bids sum above 1.

  YES basket: buy YES on all k. Pays exactly 1 if one of the listed parties wins, 0 if someone
              else does. Profit per set = 1 − Σ ask_i. Only riskless if the legs are exhaustive,
              which we never know for sure, so it is off unless ARB_YES_BASKETS=true, and then
              only for D/R races where races.csv confirms both parties have a nominee.

The engine's own relationship graph (`GET /relationships`) is empty for this tournament, so
baskets come from market titles. `violations()` still reports anything the engine flags.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..api import markets as mk
from ..api.client import SigClient
from ..api.models import Level, Market, OrderBook
from ..data.external.races import Race
from ..models.fundamentals import parse_title


@dataclass(frozen=True)
class Leg:
    market_id: str
    exchange_id: str
    label: str  # "D" | "R" | "I"
    title: str


@dataclass(frozen=True)
class Basket:
    key: str  # race key, e.g. "S-TX"
    legs: Tuple[Leg, ...]
    exhaustive: bool  # one leg is (believed) certain to win: allows the YES basket

    def payout(self, side: str) -> int:
        """Guaranteed payout per set."""
        return len(self.legs) - 1 if side == "no" else 1


@dataclass(frozen=True)
class ArbOrder:
    basket: Basket
    side: str  # "no" | "yes": the side bought on every leg
    sets: int
    limits: Tuple[float, ...]  # per leg, side-relative: worst level we take
    cost: float  # for all sets, at the levels walked

    @property
    def payout(self) -> float:
        return self.sets * self.basket.payout(self.side)

    @property
    def profit(self) -> float:
        return self.payout - self.cost


def build_baskets(markets: Iterable[Market], races: Optional[Dict[str, Race]] = None) -> List[Basket]:
    by_race: Dict[str, List[Leg]] = {}
    for m in markets:
        if not m.is_binary:
            continue
        parsed = parse_title(m.title)
        if parsed is None:
            continue
        key, party = parsed
        by_race.setdefault(key, []).append(Leg(m.id, m.yes_exchange_id, party, m.title))
    out = []
    for key, legs in sorted(by_race.items()):
        labels = sorted(l.label for l in legs)
        if len(legs) < 2 or len(set(labels)) != len(labels):
            continue
        r = (races or {}).get(key)
        exhaustive = (labels == ["D", "R"] and r is not None and r.has_d and r.has_r and not r.skip)
        out.append(Basket(key, tuple(sorted(legs, key=lambda l: l.label)), exhaustive))
    return out


def screen(basket: Basket, quotes: Dict[str, Tuple[Optional[float], Optional[float]]],
           min_profit: float, allow_yes: bool) -> Optional[str]:
    """Top-of-book check from bulk prices: quotes[exchange_id] = (best_bid, best_ask).
    If the best levels don't clear the bar, no deeper level will."""
    qs = [quotes.get(l.exchange_id, (None, None)) for l in basket.legs]
    bids = [q[0] for q in qs]
    asks = [q[1] for q in qs]
    if all(b is not None for b in bids) and sum(bids) - 1 > min_profit + 1e-9:
        return "no"
    if (allow_yes and basket.exhaustive and all(a is not None for a in asks)
            and 1 - sum(asks) > min_profit + 1e-9):
        return "yes"
    return None


def top_edge(basket: Basket, quotes: Dict[str, Tuple[Optional[float], Optional[float]]], side: str) -> float:
    """Profit per set at the top of the book: what screen() tested."""
    qs = [quotes[l.exchange_id] for l in basket.legs]
    return sum(q[0] for q in qs) - 1 if side == "no" else 1 - sum(q[1] for q in qs)


def _levels(book: OrderBook, side: str) -> List[Level]:
    return book.no_asks() if side == "no" else list(book.asks)


def _walk(ladders: List[List[Level]], profit: Callable[[List[float]], float], min_profit: float,
          max_sets: int) -> Optional[Tuple[int, Tuple[float, ...], float]]:
    """Walk every leg's ladder together (best level first), adding sets while the marginal set
    still clears min_profit. profit(prices) is the gain of one set at those per-leg prices.
    Returns (sets, worst price taken per leg, Σ over sets of Σ prices), or None."""
    if not ladders or any(not lad for lad in ladders):
        return None
    idx = [0] * len(ladders)
    left = [lad[0].quantity for lad in ladders]
    sets, total = 0, 0.0
    limits = [0.0] * len(ladders)
    while sets < max_sets:
        prices = [lad[i].price for lad, i in zip(ladders, idx)]
        if profit(prices) <= min_profit + 1e-9:
            break
        take = int(min(min(left), max_sets - sets))
        if take <= 0:
            break
        sets += take
        total += take * sum(prices)
        for j in range(len(ladders)):
            limits[j] = prices[j]
            left[j] -= take
            if left[j] < 1:  # level used up: move to the next one
                idx[j] += 1
                if idx[j] >= len(ladders[j]):
                    return (sets, tuple(limits), total) if sets else None
                left[j] = ladders[j][idx[j]].quantity
    return (sets, tuple(limits), total) if sets else None


def size(basket: Basket, side: str, books: Dict[str, OrderBook], min_profit: float,
         max_sets: int) -> Optional[ArbOrder]:
    """Buy sets while each one still clears min_profit."""
    payout = basket.payout(side)
    r = _walk([_levels(books[l.exchange_id], side) for l in basket.legs],
              lambda prices: payout - sum(prices), min_profit, max_sets)
    return ArbOrder(basket, side, r[0], r[1], r[2]) if r else None


# ---- exits: selling a NO basket we hold ----
#
# Holding a NO basket pays k−1 per set at settlement. Selling NO on every leg pays Σ(1 − ask_i)
# now, which beats holding when the YES asks sum below 1: extra riskless profit 1 − Σask per
# set, and the capital comes back. Only ever closes NO we hold, so unlike the YES basket it
# needs no assumption about who can win.

@dataclass(frozen=True)
class ExitOrder:
    basket: Basket
    sets: int
    limits: Tuple[float, ...]  # per leg: lowest NO price we sell at
    proceeds: float

    @property
    def gain(self) -> float:
        """Over holding to settlement."""
        return self.proceeds - self.sets * self.basket.payout("no")


def held_baskets(baskets: Iterable[Basket], no_held: Dict[str, float]) -> Dict[str, int]:
    """Races where we hold the same NO quantity on every leg: race → sets.
    no_held: exchange id → NO shares held (positive)."""
    out = {}
    for b in baskets:
        q = [no_held.get(l.exchange_id, 0.0) for l in b.legs]
        if min(q) >= 1 and max(q) - min(q) < 1:
            out[b.key] = int(min(q))
    return out


def screen_exit(basket: Basket, quotes: Dict[str, Tuple[Optional[float], Optional[float]]],
                min_profit: float) -> Optional[float]:
    """Gain per set over holding at the top of the book, if it clears min_profit."""
    asks = [quotes.get(l.exchange_id, (None, None))[1] for l in basket.legs]
    if any(a is None for a in asks):
        return None
    gain = 1 - sum(asks)
    return gain if gain > min_profit + 1e-9 else None


def size_exit(basket: Basket, books: Dict[str, OrderBook], held_sets: int, min_profit: float,
              max_sets: int) -> Optional[ExitOrder]:
    """Sell NO by walking each leg's YES asks (selling NO at p ≡ buying YES at 1 − p)."""
    hold = basket.payout("no")
    ladders = [[Level(round(1 - a.price, 6), a.quantity) for a in books[l.exchange_id].asks]
               for l in basket.legs]
    r = _walk(ladders, lambda prices: sum(prices) - hold, min_profit, min(held_sets, max_sets))
    return ExitOrder(basket, r[0], r[1], r[2]) if r else None


def quotes_from_prices(rows: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    return {str(r["exchangeId"]): (r.get("bestBid"), r.get("bestAsk")) for r in rows}


def violations(client: SigClient, tournament_id: str) -> List[Dict[str, Any]]:
    """Constraints the engine itself reports as violated (none are defined for now)."""
    return mk.relationship_violations(client, tournament_id)
