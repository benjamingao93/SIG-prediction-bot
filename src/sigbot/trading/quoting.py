"""Passive quoting: rest a buy-NO order on one leg of a race priced so that, if it fills, buying the
other legs' NO from the book completes a set at a profit.

For a basket with k legs (a NO set pays k−1), quoting leg i while the others would be hedged at
their current NO asks a_j = 1 − bid_j (YES bids):

    p_i = (k−1) − Σ_{j≠i} a_j − edge        (k=2: p_D = bid_R − edge)

rounded down to the tick, so a fill hedged at today's prices locks in at least `edge` per set.
The quote must sit strictly inside leg i's NO spread, NObid_i < p_i < NOask_i (NObid_i = 1 − ask_i):
at or above the NO ask it's a taker arbitrage (the arb bot's main path), at or below the NO bid
it waits behind other orders. A resting buy-NO at p is a YES offer at 1 − p, so it fills when a
buyer lifts YES at that price.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..api.models import OrderBook
from ..api.orders import MAX_PRICE, MIN_PRICE, TICK, round_to_tick
from .arb import Basket

Quotes = Dict[str, Tuple[Optional[float], Optional[float]]]  # exchange → (YES bid, YES ask)


@dataclass(frozen=True)
class QuoteSpec:
    basket: Basket
    leg: int  # index of the quoted leg in basket.legs
    price: float  # NO price we bid
    hedge_asks: Tuple[Tuple[str, float], ...]  # (exchange, NO ask) for each other leg
    score: float  # how far inside the NO spread: p − NObid

    @property
    def exchange_id(self) -> str:
        return self.basket.legs[self.leg].exchange_id

    def edge(self) -> float:
        """Profit per set if filled and hedged at hedge_asks."""
        return self.basket.payout("no") - self.price - sum(a for _, a in self.hedge_asks)


def quote_price(basket: Basket, leg: int, quotes: Quotes, edge: float) -> Optional[QuoteSpec]:
    qs = [quotes.get(l.exchange_id, (None, None)) for l in basket.legs]
    if any(b is None or a is None for b, a in qs):
        return None
    hedge = tuple((l.exchange_id, round(1 - qs[j][0], 6)) for j, l in enumerate(basket.legs) if j != leg)
    raw = basket.payout("no") - sum(a for _, a in hedge) - edge
    if raw < MIN_PRICE:
        return None
    p = round_to_tick(raw, "buy")
    no_bid, no_ask = 1 - qs[leg][1], 1 - qs[leg][0]
    if not (no_bid + 1e-9 < p < no_ask - 1e-9) or p > MAX_PRICE:
        return None
    return QuoteSpec(basket, leg, p, hedge, round(p - no_bid, 6))


def best_leg(basket: Basket, quotes: Quotes, edge: float) -> Optional[QuoteSpec]:
    specs = [s for s in (quote_price(basket, i, quotes, edge) for i in range(len(basket.legs))) if s]
    return max(specs, key=lambda s: s.score) if specs else None


def select_quotes(baskets: Iterable[Basket], quotes: Quotes, n: int, edge: float,
                  exclude: Sequence[str] = ()) -> List[QuoteSpec]:
    """The n races with the best-placed viable quote, one leg per race."""
    specs = [s for s in (best_leg(b, quotes, edge) for b in baskets if b.key not in exclude) if s]
    return sorted(specs, key=lambda s: -s.score)[:n]


def reprice(spec: QuoteSpec, hedge_books: Dict[str, OrderBook], quotes: Quotes, edge: float,
            max_size: int) -> Optional[Tuple[QuoteSpec, int]]:
    """Price from the hedge legs' fresh books and size by their top-of-book depth, so a fill can
    be hedged at the priced level. None if no longer viable."""
    fresh: Quotes = dict(quotes)
    depth = []
    for ex, _ in spec.hedge_asks:
        no_asks = hedge_books[ex].no_asks()
        if not no_asks:
            return None
        bid, ask = fresh.get(ex, (None, None))
        fresh[ex] = (round(1 - no_asks[0].price, 6), ask)
        depth.append(no_asks[0].quantity)
    s = quote_price(spec.basket, spec.leg, fresh, edge)
    if s is None:
        return None
    size = int(min([max_size] + depth))
    return (s, size) if size >= 1 else None


def hedge_caps(spec: QuoteSpec, edge: float, slippage: float) -> Dict[str, float]:
    """Most each hedge leg may cost after a fill: the priced NO ask plus the planned edge (break
    even) plus slippage, the same allowance repairs use."""
    return {ex: round(min(MAX_PRICE, a + edge + slippage), 3) for ex, a in spec.hedge_asks}


def would_fill(price: float, last_before: Optional[float], last_now: Optional[float]) -> bool:
    """Paper fill rule: a new trade printed at or through our YES offer (1 − p). Ignores queue
    position, so it overstates fills; our quote is the best offer when it's inside the spread,
    so a lifted offer would most likely have been ours."""
    return last_now is not None and last_now != last_before and last_now >= 1 - price - TICK / 10
