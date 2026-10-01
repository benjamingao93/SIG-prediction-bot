"""Order placement. Every placement carries an idempotencyKey so retries never double-place."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .client import SigClient

TICK = 0.005
MIN_PRICE = 0.005
MAX_PRICE = 0.995


def round_to_tick(price: float, action: str) -> float:
    """Round onto the 0.005 grid in the direction that never worsens our price:
    buys round down, sells round up. Clamped to [0.005, 0.995]."""
    ticks = price / TICK
    # tolerate float noise like 0.61/0.005 = 121.99999999
    ticks = math.floor(ticks + 1e-9) if action == "buy" else math.ceil(ticks - 1e-9)
    return round(min(MAX_PRICE, max(MIN_PRICE, ticks * TICK)), 3)


def place_limit(
    client: SigClient,
    exchange_id: str,
    side: str,
    action: str,
    quantity: int,
    price: float,
    tournament_id: str,
    ttl_seconds: Optional[int] = 300,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    if side not in ("yes", "no") or action not in ("buy", "sell"):
        raise ValueError(f"bad side/action {side}/{action}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    body: Dict[str, Any] = {
        "idempotencyKey": idempotency_key or str(uuid.uuid4()),
        "exchangeId": str(exchange_id),
        "side": side,
        "action": action,
        "quantity": int(quantity),
        "price": round_to_tick(price, action),
        "tournamentId": tournament_id,
    }
    if ttl_seconds:
        exp = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        body["expirationDate"] = exp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return client.post("/orders", body)


def place_multi_leg(
    client: SigClient,
    legs: List[Dict[str, Any]],
    tournament_id: str,
    ttl_seconds: Optional[int] = 30,
    idempotency_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Up to 10 limit buys, atomically: every leg is placed or none is. Placement is atomic,
    fills are not: a leg whose limit no longer crosses the book rests (`open: true`).
    legs: [{"exchangeId", "side", "quantity", "price"}]. Returns per-leg result data, in order."""
    if not 1 <= len(legs) <= 10:
        raise ValueError("multi-leg orders take 1-10 legs")
    exp = None
    if ttl_seconds:
        exp = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="milliseconds")
        exp = exp.replace("+00:00", "Z")
    body_legs = []
    for leg in legs:
        if leg["quantity"] <= 0:
            raise ValueError("quantity must be positive")
        b = {"exchangeId": str(leg["exchangeId"]), "side": leg["side"], "action": "buy",
             "quantity": int(leg["quantity"]), "price": round_to_tick(leg["price"], "buy"),
             "tournamentId": tournament_id}
        if exp:
            b["expirationDate"] = exp
        body_legs.append(b)
    resp = client.post("/orders/multi-leg", {"idempotencyKey": idempotency_key or str(uuid.uuid4()), "legs": body_legs})
    return [r.get("data") or {} for r in sorted(resp.get("results", []), key=lambda r: r.get("index", 0))]


def cancel(client: SigClient, order_id: int) -> Any:
    return client.delete(f"/orders/{order_id}")


def cancel_all(client: SigClient, tournament_id: str, market_id: Optional[str] = None) -> int:
    body: Dict[str, Any] = {"tournamentId": tournament_id}
    if market_id:
        body["marketId"] = market_id
    return client.post("/orders/cancel-all", body).get("cancelled", 0)


def open_orders(client: SigClient, tournament_id: str) -> List[Dict[str, Any]]:
    return list(client.paginate("/orders", status="open", tournamentId=tournament_id, limit=100))
