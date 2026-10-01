"""p_f: fundamentals view from data/races.csv: partisan lean + national environment + incumbency.

Expected Democratic margin in race i, in points:
    mu_i = beta_office · 2·PVI_i + gamma_office · env + incumbency_i + margin_adj_i
  - Cook PVI is a lean in vote share, so 2·PVI is a lean in margin. beta < 1 for offices that
    vote less along presidential lines.
  - env: national House-vote margin, Democratic-positive (GENERIC_BALLOT_D in .env). gamma < 1
    for offices that follow the national mood less: in 2018 (D+8.6) Republicans still won the
    governorships of SC by 8, OH by 4, IA by 3, and GA and FL narrowly.
  - incumbency: an incumbent keeps `carryover` of how far they beat the expected margin last
    time (their personal vote) when races.csv has their last result; otherwise a flat bonus.
The parameters are priors, not fitted. Fit them once markets settle, or override per race with
margin_adj.
Actual margin = mu_i + national error (shared by every race) + race error.
P(D wins) = Φ(mu_i / sqrt(sd_race² + sd_nat²)).

Chamber control integrates over the national error: given it, races are independent and the
seat count is close to normal.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..data.external.races import STATES, Race, load_races
from .base import Estimate, MarketContext

# National House popular-vote margin, Democratic-positive: the environment an incumbent last won in.
PAST_ENV = {2014: -5.7, 2016: -1.1, 2018: 8.6, 2020: 3.1, 2022: -2.8, 2024: -2.6}

# Senate seats not up in 2026 (47 D incl. King and Sanders, 53 R), minus the seats up.
SENATE_D_TOTAL = 47
SENATE_D_NEEDED = 51  # VP Vance breaks 50-50 ties for Republicans
HOUSE_NEEDED = 218

CHAMBERS = {"U.S. House": "CH-HOUSE", "U.S. Senate": "CH-SENATE"}
_TITLE = re.compile(r"Will the (Democratic|Republican|Independent) Party win the (.+?)\??\s*$")


@dataclass(frozen=True)
class Params:
    beta: Dict[str, float] = field(default_factory=lambda: {"house": 1.0, "senate": 0.95, "governor": 0.85})
    gamma: Dict[str, float] = field(default_factory=lambda: {"house": 1.0, "senate": 0.9, "governor": 0.6})
    flat_incumbency: Dict[str, float] = field(default_factory=lambda: {"house": 2.0, "senate": 2.0, "governor": 4.0})
    carryover: float = 0.5
    race_sd: Dict[str, float] = field(default_factory=lambda: {"house": 7.0, "senate": 8.0, "governor": 10.0})
    national_sd: float = 3.5
    uncertainty: float = 0.08  # Estimate.uncertainty for a fundamentals-only view


def parse_title(title: str) -> Optional[Tuple[str, str]]:
    """'Will the Democratic Party win the PA-07 House race?' → ('H-PA-07', 'D')."""
    m = _TITLE.match(title.strip())
    if not m:
        return None
    party, target = m.group(1)[0], m.group(2).strip()
    if target in CHAMBERS:
        return CHAMBERS[target], party
    h = re.fullmatch(r"(\w\w)-(\d+|AL) House race", target)
    if h:
        d = h.group(2)
        return f"H-{h.group(1)}-{d.zfill(2) if d.isdigit() else d}", party
    o = re.fullmatch(r"(.+) (Senate|Governor)", target)
    if o and o.group(1) in STATES:
        return f"{o.group(2)[0]}-{STATES[o.group(1)]}", party
    return None


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class FundamentalsModel:
    def __init__(self, races: Dict[str, Race], env: float, params: Params = Params()):
        self.env = env
        self.params = params
        self.path: Optional[Path] = None
        self._mtime = 0.0
        self.set_races(races)

    @classmethod
    def from_file(cls, path: Path, env: float, params: Params = Params()) -> "FundamentalsModel":
        m = cls(load_races(path), env, params)
        m.path, m._mtime = path, path.stat().st_mtime
        return m

    def set_races(self, races: Dict[str, Race]) -> None:
        self.races = races
        self._chamber = {"CH-HOUSE": self._house_control(), "CH-SENATE": self._senate_control()}

    def refresh(self) -> bool:
        """Reload races.csv if it changed on disk (your margin_adj / skip edits)."""
        if self.path is None or not self.path.exists():
            return False
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        self.set_races(load_races(self.path))
        return True

    # ---- single race ----

    def margin(self, r: Race) -> float:
        p = self.params
        mu = p.beta[r.office] * 2 * r.pvi_d + p.gamma[r.office] * self.env + r.margin_adj
        if r.inc_running and r.inc_party in ("D", "R"):
            sign = 1 if r.inc_party == "D" else -1
            bonus = p.flat_incumbency[r.office]
            past_env = PAST_ENV.get(r.last_year or 0)
            if r.last_margin_d is not None and past_env is not None:
                personal = r.last_margin_d - (p.beta[r.office] * 2 * r.pvi_d + p.gamma[r.office] * past_env)
                bonus = max(bonus, sign * p.carryover * personal)
            mu += sign * bonus
        return mu

    def sd(self, office: str) -> float:
        return math.hypot(self.params.race_sd[office], self.params.national_sd)

    def p_dem(self, r: Race, national_shift: Optional[float] = None) -> float:
        if not r.has_d:
            return 0.0
        if not r.has_r:
            return 1.0
        if national_shift is None:
            return phi(self.margin(r) / self.sd(r.office))
        return phi((self.margin(r) + national_shift) / self.params.race_sd[r.office])

    # ---- chambers ----

    def _control(self, office: str, d_fixed: int, needed: int) -> Optional[float]:
        rs = [r for r in self.races.values() if r.office == office]
        if not rs:
            return None
        sd_n, n = self.params.national_sd, 201
        total = wsum = 0.0
        for k in range(n):
            z = -5 + 10 * k / (n - 1)
            w = math.exp(-z * z / 2)
            ps = [self.p_dem(r, z * sd_n) for r in rs]
            mean = d_fixed + sum(ps)
            var = sum(p * (1 - p) for p in ps) or 1e-9
            total += w * (1 - phi((needed - 0.5 - mean) / math.sqrt(var)))
            wsum += w
        return total / wsum

    def _house_control(self) -> Optional[float]:
        return self._control("house", 0, HOUSE_NEEDED)

    def _senate_control(self) -> Optional[float]:
        d_up = sum(1 for r in self.races.values() if r.office == "senate" and r.inc_party == "D")
        return self._control("senate", SENATE_D_TOTAL - d_up, SENATE_D_NEEDED)

    # ---- ProbabilityModel ----

    def p_yes(self, title: str) -> Optional[float]:
        parsed = parse_title(title)
        if parsed is None:
            return None
        key, party = parsed
        if party == "I":
            return None
        if key in self._chamber:
            p = self._chamber[key]
        else:
            r = self.races.get(key)
            # Skip races you flagged and races missing a major-party nominee (an independent
            # is the real opponent, or a top-two runoff between one party's candidates).
            if r is None or r.skip or not (r.has_d and r.has_r):
                return None
            p = self.p_dem(r)
        if p is None:
            return None
        return p if party == "D" else 1 - p

    def predict(self, ctx: MarketContext) -> Optional[Estimate]:
        p = self.p_yes(ctx.title)
        if p is None:
            return None
        return Estimate(min(max(p, 0.005), 0.995), self.params.uncertainty, "fundamentals")

    def group(self, title: str) -> Optional[str]:
        parsed = parse_title(title)
        if parsed is None:
            return None
        key = parsed[0]
        return {"CH": "chamber", "H": "house", "S": "senate", "G": "governor"}[key.split("-")[0]]
