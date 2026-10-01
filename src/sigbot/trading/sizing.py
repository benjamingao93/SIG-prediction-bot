"""Fractional Kelly for a binary contract bought at price q with win probability p."""
from __future__ import annotations

import math


def kelly_fraction(p: float, q: float) -> float:
    """f* = (p - q) / (1 - q), floored at 0."""
    if q >= 1 or p <= q:
        return 0.0
    return (p - q) / (1 - q)


def kelly_shares(p: float, q: float, bankroll: float, fraction: float = 0.25) -> int:
    stake = bankroll * fraction * kelly_fraction(p, q)
    return int(math.floor(stake / q)) if q > 0 else 0
