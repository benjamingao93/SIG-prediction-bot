"""Market discovery and market data reads."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .client import SigClient
from .models import Market, OrderBook


def get_tournament(client: SigClient, slug: str) -> Dict[str, Any]:
    return client.get(f"/tournaments/{slug}")


def list_tournaments(client: SigClient) -> List[Dict[str, Any]]:
    resp = client.get("/tournaments")
    return resp.get("data", resp) if isinstance(resp, dict) else resp


def list_tournament_markets(client: SigClient, slug: str, status: Optional[str] = None) -> List[Market]:
    return [Market.from_api(m) for m in client.paginate(f"/tournaments/{slug}/markets", status=status, limit=100)]


def get_orderbook(client: SigClient, exchange_id: str, tournament_id: str, depth: int = 20) -> OrderBook:
    return OrderBook.from_api(
        client.get(f"/exchanges/{exchange_id}/orderbook", depth=depth, tournamentId=tournament_id)
    )


def bulk_prices(client: SigClient, exchange_ids: Sequence[str], tournament_id: str) -> List[Dict[str, Any]]:
    """One read per 100 exchanges: latestPrice, bestBid, bestAsk, spread."""
    out: List[Dict[str, Any]] = []
    ids = list(exchange_ids)
    for i in range(0, len(ids), 100):
        resp = client.get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=tournament_id)
        out.extend(resp.get("data", []))
    return out


def relationship_violations(client: SigClient, tournament_id: str) -> List[Dict[str, Any]]:
    resp = client.get("/relationships/constraints", tournamentId=tournament_id, violationsOnly="true")
    return resp.get("data", [])
