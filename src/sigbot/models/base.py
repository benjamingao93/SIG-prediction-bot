"""Model interface. Models estimate probabilities only. They never see order books,
sizes or positions, so P&L can later be attributed to the model or to execution."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

from ..data.external.inputs import RaceInputs


@dataclass(frozen=True)
class Estimate:
    p_yes: float
    uncertainty: float  # ~1 s.d. of p_yes; widens the required edge
    source: str = ""


@dataclass(frozen=True)
class MarketContext:
    market_id: str
    title: str
    p_market: Optional[float]  # mid (or last) price: a scalar, not the book
    inputs: Optional[RaceInputs]
    days_to_settle: Optional[float]
    now: datetime


class ProbabilityModel(Protocol):
    def predict(self, ctx: MarketContext) -> Optional[Estimate]: ...


def clip(p: float, eps: float = 1e-4) -> float:
    return min(1 - eps, max(eps, p))


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))
