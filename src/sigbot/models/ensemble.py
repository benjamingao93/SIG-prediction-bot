"""p_c: combine market and outside view.

v0 (no fitted coefficients): p_c = w*p_m + (1-w)*p_e, w=0.7 by default.
v1 (after markets resolve): fit
    logit(p) = b0 + b1*logit(p_m) + b2*(p_poll - p_m) + b3*(p_fc - p_m) + b4*days_to_settle
with `Ensemble.fit` and pass the coefficients in.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from .base import Estimate, MarketContext, logit, sigmoid
from .external import ExternalModel
from .fundamentals import FundamentalsModel


class Ensemble:
    def __init__(self, w_market: float = 0.7, coefs: Optional[Sequence[float]] = None,
                 fundamentals: Optional[FundamentalsModel] = None):
        self.w_market = w_market
        self.coefs = list(coefs) if coefs else None
        self.external = ExternalModel(fundamentals)

    @staticmethod
    def features(ctx: MarketContext) -> Optional[Tuple[float, float, float, float]]:
        if ctx.p_market is None or ctx.inputs is None:
            return None
        pm = ctx.p_market
        poll = ctx.inputs.p_poll if ctx.inputs.p_poll is not None else pm
        fc = ctx.inputs.p_forecast if ctx.inputs.p_forecast is not None else pm
        return (logit(pm), poll - pm, fc - pm, ctx.days_to_settle or 0.0)

    def predict(self, ctx: MarketContext) -> Optional[Estimate]:
        ext = self.external.predict(ctx)
        if ext is None or ctx.p_market is None:
            return None  # no outside view → no opinion → no trade
        x = self.features(ctx) if self.coefs else None
        if x is not None:
            b = self.coefs
            z = b[0] + sum(bi * xi for bi, xi in zip(b[1:], x))
            p = sigmoid(z)
        else:
            p = self.w_market * ctx.p_market + (1 - self.w_market) * ext.p_yes
        unc = (1 - self.w_market) * ext.uncertainty + 0.01
        return Estimate(p, unc, "ensemble")

    @staticmethod
    def fit(rows: Sequence[Tuple[MarketContext, int]]) -> list:
        """rows: (context at some snapshot time, outcome 0/1). Needs `pip install -e '.[ml]'`."""
        from sklearn.linear_model import LogisticRegression

        X, y = [], []
        for ctx, outcome in rows:
            f = Ensemble.features(ctx)
            if f is not None:
                X.append(f)
                y.append(outcome)
        lr = LogisticRegression(C=1.0).fit(X, y)
        return [float(lr.intercept_[0])] + [float(c) for c in lr.coef_[0]]
