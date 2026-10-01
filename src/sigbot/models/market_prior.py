"""p_m: the market's own price. Trading on this alone produces zero edge, by design."""
from __future__ import annotations

from typing import Optional

from .base import Estimate, MarketContext


class MarketPrior:
    def __init__(self, uncertainty: float = 0.05):
        self.uncertainty = uncertainty

    def predict(self, ctx: MarketContext) -> Optional[Estimate]:
        if ctx.p_market is None:
            return None
        return Estimate(ctx.p_market, self.uncertainty, "market")
