"""`sigbot convergence`: how fast do SIG's mispricings close? Read from the edge history the arb
bot records every DIR_HISTORY_EVERY seconds.

An episode starts when a view first clears the bar with the sources agreeing. It is followed
until the edge has halved and until it has closed (≤ CLOSED). At the close it notes whether SIG's
price moved to the fair value (what pays a short-hold trade) or the fair value moved to SIG's price
(Kalshi or the ratings came round: holding would not have paid).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

CLOSED = 0.005


@dataclass
class Episode:
    race: str
    view: str
    start: datetime
    edge0: float
    price0: float
    fair0: float
    half: Optional[float] = None  # hours until edge ≤ edge0 / 2
    closed: Optional[float] = None  # hours until edge ≤ CLOSED
    by_price: Optional[float] = None  # share of the closing that came from SIG's price moving
    observed: float = 0.0  # hours of history after the start


def episodes(rows: Sequence[Dict]) -> List[Episode]:
    """rows: edge-history dicts with ts, race, view, price, fair, edge, required, agree."""
    series: Dict[Tuple[str, str], List[Dict]] = {}
    for r in rows:
        series.setdefault((r["race"], r["view"]), []).append(r)
    out: List[Episode] = []
    for (race, view), rs in series.items():
        rs.sort(key=lambda r: r["ts"])
        open_ep: Optional[Episode] = None
        for r in rs:
            t = datetime.fromisoformat(r["ts"])
            live = r["edge"] > r["required"] and bool(r["agree"])
            if open_ep is None:
                if live:
                    open_ep = Episode(race, view, t, r["edge"], r["price"], r["fair"])
                    out.append(open_ep)
                continue
            hours = (t - open_ep.start).total_seconds() / 3600
            open_ep.observed = hours
            if open_ep.half is None and r["edge"] <= open_ep.edge0 / 2:
                open_ep.half = hours
            if r["edge"] <= CLOSED:
                open_ep.closed = hours
                moved_price = r["price"] - open_ep.price0  # NO price rising toward fair
                moved_fair = open_ep.fair0 - r["fair"]  # fair falling toward price
                total = abs(moved_price) + abs(moved_fair)
                open_ep.by_price = abs(moved_price) / total if total else None
                open_ep = None  # a later re-opening is a new episode
    return out


def summary(eps: List[Episode]) -> str:
    if not eps:
        return "No episodes yet: let the bot record edge history for a while (every 10 min)."
    lines = [f"{len(eps)} episodes (a view clearing the bar with sources agreeing)"]
    halves = [e.half for e in eps if e.half is not None]
    if halves:
        lines.append(f"edge halved: {len(halves)} of {len(eps)}, median after {statistics.median(halves):.1f} h")
    for h in (1, 6, 24):
        seen = [e for e in eps if e.observed >= h or (e.closed is not None and e.closed <= h)]
        if seen:
            n = sum(1 for e in seen if e.closed is not None and e.closed <= h)
            lines.append(f"closed within {h:>2} h: {n} of {len(seen)} observed that long ({n / len(seen):.0%})")
    via = [e.by_price for e in eps if e.by_price is not None]
    if via:
        lines.append(f"when closed, SIG's price did {statistics.mean(via):.0%} of the moving on average "
                     f"(the rest was Kalshi/ratings moving toward SIG)")
    lines.append("\nrace     view  start(UTC)        edge0  halved  closed  by price")
    for e in sorted(eps, key=lambda e: e.start)[-30:]:
        f = lambda v, u="h": "    –" if v is None else f"{v:5.1f}{u}"
        lines.append(f"{e.race:8} {e.view:4}  {e.start:%m-%d %H:%M}   {e.edge0:+.3f}  {f(e.half)}  {f(e.closed)}  "
                     f"{'   –' if e.by_price is None else f'{e.by_price:4.0%}'}")
    return "\n".join(lines)
