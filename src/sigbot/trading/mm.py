"""Market making on one market at a time, anchored on the fair value. Pure: no I/O.

We quote both sides of a party's YES market: a bid (buy YES) below the fair value, and an ask,
which on SIG is a buy of NO at 1 − ask. Once both have filled, the YES and NO cancel on the
exchange and pay 1 immediately: the spread (ask − bid) is banked and the cash is back at once,
with nothing left waiting for settlement. Inventory left over between fills is exposure, so the
quotes lean against it (skew) and the side that would add to a full inventory stops quoting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

TICK = 0.005


def _down(p: float) -> float:
    return round(math.floor(p / TICK + 1e-9) * TICK, 3)


def _up(p: float) -> float:
    return round(math.ceil(p / TICK - 1e-9) * TICK, 3)


@dataclass
class Inventory:
    """YES and NO shares held in one market. Pairs cancel (pay 1) as soon as both sides are held."""
    yes: float = 0.0
    yes_cost: float = 0.0
    no: float = 0.0
    no_cost: float = 0.0
    realized: float = 0.0
    pairs: float = 0.0

    @property
    def net(self) -> float:
        """YES-equivalent exposure: + long YES, − long NO."""
        return self.yes - self.no

    def fill(self, side: str, qty: float, price: float) -> float:
        """Add a fill (price is what that side cost); cancel pairs. Returns the profit banked."""
        if side == "yes":
            self.yes, self.yes_cost = self.yes + qty, self.yes_cost + qty * price
        else:
            self.no, self.no_cost = self.no + qty, self.no_cost + qty * price
        n = min(self.yes, self.no)
        if n <= 0:
            return 0.0
        y_avg, n_avg = self.yes_cost / self.yes, self.no_cost / self.no
        banked = n * (1 - y_avg - n_avg)
        self.yes, self.yes_cost = self.yes - n, self.yes_cost - n * y_avg
        self.no, self.no_cost = self.no - n, self.no_cost - n * n_avg
        self.realized += banked
        self.pairs += n
        return banked

    def unrealized(self, fair: float) -> float:
        """What the open side is worth at the fair value, over its cost."""
        return self.yes * fair - self.yes_cost + self.no * (1 - fair) - self.no_cost

    def as_dict(self) -> Dict[str, float]:
        return {k: getattr(self, k) for k in ("yes", "yes_cost", "no", "no_cost", "realized", "pairs")}


def quote_prices(fair: float, bid: Optional[float], ask: Optional[float], net: float,
                 edge: float, max_inv: float, skew: float) -> Tuple[Optional[float], Optional[float]]:
    """(our bid, our ask) in YES terms, or None for a side we don't quote.

    Centred on the fair value, shifted against inventory (long YES → both lower). Each side steps
    one tick inside the book when that still keeps `edge` from the centre, sits at the edge
    otherwise, and never crosses the other side of the book (we only rest, never take)."""
    fill = max(-1.0, min(1.0, net / max_inv))  # how full the inventory is, signed
    centre = fair - skew * fill
    want_bid, want_ask = _down(centre - edge), _up(centre + edge)
    b = want_bid if bid is None else min(want_bid, round(bid + TICK, 3))
    a = want_ask if ask is None else max(want_ask, round(ask - TICK, 3))
    # The side that unwinds inventory gets keener as it fills: from a tick inside the book toward
    # the centre ± edge, so a full inventory is offered at the tightest price that still has edge.
    if fill > 0 and a > want_ask:
        a = _up(a - (a - want_ask) * fill)
    elif fill < 0 and b < want_bid:
        b = _down(b + (want_bid - b) * -fill)
    if ask is not None and b >= ask:
        b = round(ask - TICK, 3)
    if bid is not None and a <= bid:
        a = round(bid + TICK, 3)
    b = b if net < max_inv and TICK <= b < 1 else None
    a = a if net > -max_inv and 0 < a <= 1 - TICK else None
    if b is not None and a is not None and a - b < TICK:
        return None, None
    return b, a


def capture(fair: float, bid: Optional[float], ask: Optional[float], edge: float) -> float:
    """Spread we'd earn per round trip with both quotes at the top of the book (0 if either can't be)."""
    if bid is None or ask is None:
        return 0.0
    b, a = quote_prices(fair, bid, ask, 0.0, edge, 1.0, 0.0)
    if b is None or a is None or b <= bid or a >= ask:
        return 0.0  # behind the book on a side: it would rarely fill
    return a - b
