"""p_e: outside view from polls/forecasts in data/inputs.csv and the fundamentals model."""
from __future__ import annotations

from typing import List, Optional, Tuple

from .base import Estimate, MarketContext, logit, sigmoid
from .fundamentals import FundamentalsModel


class ExternalModel:
    def __init__(self, fundamentals: Optional[FundamentalsModel] = None):
        self.fundamentals = fundamentals

    def predict(self, ctx: MarketContext) -> Optional[Estimate]:
        srcs: List[Tuple[float, float]] = []  # (p, uncertainty)
        inp = ctx.inputs
        if inp is not None:
            srcs += [(p, 0.05) for p in (inp.p_poll, inp.p_forecast) if p is not None]
        if self.fundamentals is not None:
            f = self.fundamentals.predict(ctx)
            if f is not None:
                srcs.append((f.p_yes, f.uncertainty))
        if not srcs:
            return None
        ps = [p for p, _ in srcs]
        p = sigmoid(sum(logit(x) for x in ps) / len(ps))
        # one source: its own uncertainty; several that disagree: less sure
        unc = srcs[0][1] if len(srcs) == 1 else 0.03 + (max(ps) - min(ps)) / 2
        return Estimate(p, unc, "external")
