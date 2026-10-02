"""Fair value for two-party races from Kalshi (real money) and the forecasters' ratings, and the
edge SIG's prices give against it.

Kalshi: the two-party mid, P(D) = mid_D / (mid_D + mid_R), used only when both parties' quotes
are tight (spread ≤ max_kalshi_spread), have enough open interest, and other candidates are
priced near zero (≤ max_other): a strong independent makes "D vs R" the wrong question.
Ratings: the forecasters' average P(D) from races.csv, with at least min_raters ratings.
Blend: log-odds, kalshi_weight on Kalshi. Uncertainty: the Kalshi spread and how far the two
sources disagree; a ratings-only value is coarse, so it carries a bigger uncertainty.

A view is always expressed by buying NO on the other party ("D underpriced" = buy NO on R at
1 − bid_R). That never buys YES on a market where the arb bot's baskets hold NO, which would
cancel against them. P(NO on R pays) = P(D wins) when only D and R can win.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..config import DirectionalConfig
from ..data.external.races import Race
from .base import logit, sigmoid


@dataclass(frozen=True)
class FairValue:
    race: str
    p_d: float  # blended P(Democrat wins)
    kalshi_p_d: Optional[float]
    kalshi_spread: Optional[float]
    rating_p_d: Optional[float]
    uncertainty: float
    sources: str  # "kalshi+ratings" | "kalshi" | "ratings"


@dataclass(frozen=True)
class Edge:
    race: str
    view: str  # "D" or "R": the party we think SIG underprices
    buy_exchange: str  # the OTHER party's market: we buy NO there
    price: float  # NO price paid: 1 − that market's YES bid
    fair: float  # P(the view's party wins)
    edge: float  # fair − price, per share
    required: float  # min_edge + uncertainty_mult × uncertainty
    agree: bool  # every available source alone also sees a positive edge
    sig_p_d: Optional[float]  # SIG's own mid for the D market, for reference
    fv: FairValue

    @property
    def tradeable(self) -> bool:
        return self.edge > self.required

    def as_row(self) -> Dict[str, Any]:
        return {"race": self.race, "view": self.view, "price": round(self.price, 4), "fair": round(self.fair, 4),
                "edge": round(self.edge, 4), "required": round(self.required, 4), "agree": self.agree,
                "tradeable": self.tradeable, "sig_p_d": self.sig_p_d, "kalshi_p_d": self.fv.kalshi_p_d,
                "kalshi_spread": self.fv.kalshi_spread, "rating_p_d": self.fv.rating_p_d,
                "p_d": round(self.fv.p_d, 4), "uncertainty": round(self.fv.uncertainty, 4), "sources": self.fv.sources}


def kalshi_two_party(quotes: Mapping[str, Mapping[str, Any]], cfg: DirectionalConfig) -> Optional[Tuple[float, float]]:
    """(P(D), worse spread) from Kalshi's D and R quotes, or None if they can't be trusted."""
    d, r, o = quotes.get("D"), quotes.get("R"), quotes.get("O")
    if not d or not r:
        return None
    if None in (d.get("bid"), d.get("ask"), r.get("bid"), r.get("ask")):
        return None
    spread = max(d["ask"] - d["bid"], r["ask"] - r["bid"])
    if spread > cfg.max_kalshi_spread + 1e-9:
        return None
    if (d.get("open_interest") or 0) + (r.get("open_interest") or 0) < cfg.min_kalshi_oi:
        return None
    if o and o.get("bid") is not None and o.get("ask") is not None and (o["bid"] + o["ask"]) / 2 > cfg.max_other:
        return None
    mid_d, mid_r = (d["bid"] + d["ask"]) / 2, (r["bid"] + r["ask"]) / 2
    if mid_d + mid_r <= 0:
        return None
    return mid_d / (mid_d + mid_r), spread


def fair_value(race: Race, kalshi: Optional[Mapping[str, Mapping[str, Any]]],
               cfg: DirectionalConfig) -> Optional[FairValue]:
    k = kalshi_two_party(kalshi, cfg) if kalshi else None
    rp = race.rating_p_d if race.rating_n >= cfg.min_raters else None
    if k and rp is not None:
        p = sigmoid(cfg.kalshi_weight * logit(k[0]) + (1 - cfg.kalshi_weight) * logit(rp))
        return FairValue(race.race, p, k[0], k[1], rp, k[1] / 2 + abs(k[0] - rp) / 2, "kalshi+ratings")
    if k:
        return FairValue(race.race, k[0], k[0], k[1], None, k[1] / 2 + 0.02, "kalshi")
    if rp is not None:
        return FairValue(race.race, rp, None, None, rp, 0.05 + (race.rating_spread or 0) / 2, "ratings")
    return None


def edges(fv: FairValue, d_ex: str, r_ex: str, sig: Mapping[str, Tuple[Optional[float], Optional[float]]],
          cfg: DirectionalConfig) -> List[Edge]:
    """Both views for one race. sig[exchange] = (YES bid, YES ask) on SIG."""
    (d_bid, d_ask), (r_bid, r_ask) = sig.get(d_ex, (None, None)), sig.get(r_ex, (None, None))
    sig_p_d = (d_bid + d_ask) / 2 if d_bid is not None and d_ask is not None else None
    required = cfg.min_edge + cfg.uncertainty_mult * fv.uncertainty
    out = []
    for view, other_ex, other_bid, fair, sources in (
            ("D", r_ex, r_bid, fv.p_d, (fv.kalshi_p_d, fv.rating_p_d)),
            ("R", d_ex, d_bid, 1 - fv.p_d, tuple(None if s is None else 1 - s for s in (fv.kalshi_p_d, fv.rating_p_d)))):
        if other_bid is None:
            continue
        price = round(1 - other_bid, 6)
        agree = all(s - price > 0 for s in sources if s is not None)
        out.append(Edge(fv.race, view, other_ex, price, fair, fair - price, required, agree, sig_p_d, fv))
    return out
