"""Fair probability vs the executable book → which side to buy, at what limit, how many.

Trade only when edge = p_side - price > min_edge + k * uncertainty, walking the book
level by level so deeper (worse) levels are only taken while they still clear the bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..api.models import Level, OrderBook
from ..models.base import Estimate


@dataclass(frozen=True)
class Signal:
    market_id: str
    exchange_id: str
    side: str  # "yes" | "no"
    limit_price: float  # worst level we're willing to take
    quantity: int  # total available at levels that clear the threshold
    avg_price: float
    p_side: float  # our probability for the side we're buying
    threshold: float

    @property
    def edge(self) -> float:
        return self.p_side - self.avg_price

    @property
    def expected_value(self) -> float:
        return self.edge * self.quantity


def walk(p_side: float, asks: List[Level], threshold: float) -> Optional[tuple]:
    qty, cost, limit = 0.0, 0.0, None
    for lvl in asks:
        if p_side - lvl.price <= threshold:
            break
        qty += lvl.quantity
        cost += lvl.price * lvl.quantity
        limit = lvl.price
    if qty <= 0 or limit is None:
        return None
    return limit, int(qty), cost / qty


def evaluate(est: Estimate, book: OrderBook, min_edge: float, uncertainty_mult: float = 1.0) -> Optional[Signal]:
    threshold = min_edge + uncertainty_mult * est.uncertainty
    candidates = []
    for side, p_side, asks in (("yes", est.p_yes, book.asks), ("no", 1 - est.p_yes, book.no_asks())):
        r = walk(p_side, asks, threshold)
        if r:
            limit, qty, avg = r
            candidates.append(Signal(book.market_id, book.exchange_id, side, limit, qty, avg, p_side, threshold))
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.expected_value)
