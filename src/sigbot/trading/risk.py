"""Hard limits that override whatever Kelly says."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from ..api.models import Position
from ..config import RiskLimits


@dataclass
class Decision:
    allowed_cost: float
    reason: str


class RiskManager:
    def __init__(self, limits: RiskLimits, kill_switch: Path, starting_equity: Optional[float] = None):
        self.limits = limits
        self.kill_switch = kill_switch
        self.starting_equity = starting_equity
        self.by_exchange: Dict[str, float] = defaultdict(float)
        self.by_market: Dict[str, float] = defaultdict(float)
        self.by_group: Dict[str, float] = defaultdict(float)

    def load_positions(self, positions: Iterable[Position], group_of: Dict[str, str]) -> None:
        """Rebuild exposure from authoritative positions (cost basis)."""
        self.by_exchange.clear()
        self.by_market.clear()
        self.by_group.clear()
        for p in positions:
            if p.settled or p.quantity == 0:
                continue
            self.add(p.exchange_id, p.market_id, group_of.get(p.market_id, "ungrouped"), abs(p.cost_basis))

    def add(self, exchange_id: str, market_id: str, group: str, cost: float) -> None:
        # Conservative: an opposite-side buy nets on the exchange, but we still count it.
        self.by_exchange[exchange_id] += cost
        self.by_market[market_id] += cost
        self.by_group[group] += cost

    def check(self, exchange_id: str, market_id: str, group: str, cash: float, equity: Optional[float]) -> Decision:
        L = self.limits
        if self.kill_switch.exists():
            return Decision(0.0, f"kill switch {self.kill_switch} present")
        if equity is not None and self.starting_equity is not None \
                and self.starting_equity - equity >= L.daily_loss_stop:
            return Decision(0.0, f"loss stop: equity {equity:.0f} vs start {self.starting_equity:.0f}")
        room = {
            "position": L.max_position - self.by_exchange[exchange_id],
            "market": L.max_market_exposure - self.by_market[market_id],
            "group": L.max_group_exposure - self.by_group[group],
            "order": L.max_order_cost,
            "cash": cash,
        }
        binding = min(room, key=room.get)
        return Decision(max(0.0, room[binding]), f"bound by {binding}")
