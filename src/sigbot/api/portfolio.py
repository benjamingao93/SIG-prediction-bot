"""Account, positions and P&L reads (tournament-scoped)."""
from __future__ import annotations

from typing import Any, Dict, List

from .client import SigClient
from .models import Position


def account(client: SigClient) -> Dict[str, Any]:
    return client.get("/account")


def positions(client: SigClient, slug: str) -> List[Position]:
    resp = client.get(f"/tournaments/{slug}/portfolio/positions")
    return [Position.from_api(p) for p in resp.get("positions", [])]


def pnl(client: SigClient, slug: str, period: str = "all") -> Dict[str, Any]:
    return client.get(f"/tournaments/{slug}/portfolio/pnl", period=period)
