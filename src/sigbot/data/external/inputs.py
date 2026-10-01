"""Your outside information, one row per market, kept in data/inputs.csv.

Columns:
  market_id   - from `sigbot markets`
  title       - for your reference only
  p_poll      - P(YES) implied by your polling model (blank if none)
  p_forecast  - P(YES) from a published forecast (538-style, Cook, etc.) (blank if none)
  group       - correlation bucket, e.g. "senate", "house-pa", "national"; risk caps apply per group
  notes       - free text

Blank p_poll and p_forecast means "no view": the bot will not trade that market.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from ...api.models import Market

COLUMNS = ["market_id", "title", "p_poll", "p_forecast", "group", "notes"]


@dataclass(frozen=True)
class RaceInputs:
    market_id: str
    p_poll: Optional[float]
    p_forecast: Optional[float]
    group: str


def _prob(v: str) -> Optional[float]:
    v = (v or "").strip().rstrip("%")
    if not v:
        return None
    p = float(v)
    if p > 1:  # allow "72" or "72%"
        p /= 100
    if not 0 < p < 1:
        raise ValueError(f"probability out of range: {v}")
    return p


def load_inputs(path: Path) -> Dict[str, RaceInputs]:
    if not path.exists():
        return {}
    out: Dict[str, RaceInputs] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            mid = (row.get("market_id") or "").strip()
            if not mid:
                continue
            out[mid] = RaceInputs(
                market_id=mid,
                p_poll=_prob(row.get("p_poll", "")),
                p_forecast=_prob(row.get("p_forecast", "")),
                group=(row.get("group") or "").strip() or "ungrouped",
            )
    return out


def write_template(path: Path, markets: Iterable[Market]) -> int:
    """Add rows for markets not already in the file. Never overwrites your numbers."""
    existing = load_inputs(path)
    new = [m for m in markets if m.id not in existing]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(COLUMNS)
        for m in new:
            w.writerow([m.id, m.title, "", "", "", ""])
    return len(new)
