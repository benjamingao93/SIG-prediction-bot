"""p_e: outside view from polls/forecasts in data/inputs.csv."""
from __future__ import annotations

from typing import Optional

from .base import Estimate, MarketContext, logit, sigmoid


class ExternalModel:
    def predict(self, ctx: MarketContext) -> Optional[Estimate]:
        inp = ctx.inputs
        if inp is None:
            return None
        ps = [p for p in (inp.p_poll, inp.p_forecast) if p is not None]
        if not ps:
            return None
        p = sigmoid(sum(logit(x) for x in ps) / len(ps))
        # one source: less sure; two sources that disagree: less sure
        spread = abs(ps[0] - ps[-1])
        unc = (0.05 if len(ps) == 1 else 0.03) + spread / 2
        return Estimate(p, unc, "external")
