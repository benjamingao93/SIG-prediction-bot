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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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


def size(basket: Basket, side: str, books: Dict[str, OrderBook], min_profit: float,
         max_sets: int) -> Optional[ArbOrder]:
    """Walk every leg's book together, adding sets while each one still clears min_profit."""
    ladders = [_levels(books[l.exchange_id], side) for l in basket.legs]
    if any(not lad for lad in ladders):
        return None
    payout = basket.payout(side)
    idx = [0] * len(ladders)
    left = [lad[0].quantity for lad in ladders]
    sets, cost = 0, 0.0
    limits = [0.0] * len(ladders)
    while sets < max_sets:
        prices = [lad[i].price for lad, i in zip(ladders, idx)]
        if payout - sum(prices) <= min_profit + 1e-9:
            break
        take = int(min(min(left), max_sets - sets))
        if take <= 0:
            break
        sets += take
        cost += take * sum(prices)
        for j in range(len(ladders)):
            limits[j] = prices[j]
            left[j] -= take
            if left[j] < 1:  # level used up: move to the next one
                idx[j] += 1
                if idx[j] >= len(ladders[j]):
                    return ArbOrder(basket, side, sets, tuple(limits), cost) if sets else None
                left[j] = ladders[j][idx[j]].quantity
    return ArbOrder(basket, side, sets, tuple(limits), cost) if sets else None


def quotes_from_prices(rows: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    return {str(r["exchangeId"]): (r.get("bestBid"), r.get("bestAsk")) for r in rows}


def violations(client: SigClient, tournament_id: str) -> List[Dict[str, Any]]:
    """Constraints the engine itself reports as violated (none are defined for now)."""
    return mk.relationship_violations(client, tournament_id)
