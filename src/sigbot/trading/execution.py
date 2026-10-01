"""Turns sized signals into orders (live) or log lines (paper).

Limit orders only, with an expirationDate so stale quotes clean themselves up without
spending write budget on cancels. A per-exchange cooldown keeps us from re-firing every cycle.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from ..api import orders
from ..api.client import SigAPIError, SigClient
from ..data.db import DB
from ..models.base import Estimate
from .signals import Signal

log = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        client: Optional[SigClient],
        db: DB,
        tournament_id: str,
        live: bool,
        order_ttl_seconds: int = 120,
        cooldown_seconds: float = 90.0,
    ):
        if live and client is None:
            raise ValueError("live execution needs a client")
        self.client = client
        self.db = db
        self.tournament_id = tournament_id
        self.live = live
        self.order_ttl_seconds = order_ttl_seconds
        self.cooldown_seconds = cooldown_seconds
        self._last_sent: Dict[str, float] = {}

    def cooling_down(self, exchange_id: str) -> bool:
        t = self._last_sent.get(exchange_id)
        return t is not None and time.monotonic() - t < self.cooldown_seconds

    def submit(self, sig: Signal, quantity: int, est: Estimate, p_market: Optional[float]) -> bool:
        """True if the order was sent (live) or logged (paper)."""
        if quantity <= 0 or self.cooling_down(sig.exchange_id):
            return False
        self._last_sent[sig.exchange_id] = time.monotonic()
        row = dict(
            mode="live" if self.live else "paper", market_id=sig.market_id, exchange_id=sig.exchange_id,
            side=sig.side, action="buy", price=sig.limit_price, quantity=quantity,
            p_model=est.p_yes, p_market=p_market, uncertainty=est.uncertainty, edge=sig.edge,
        )
        msg = (f"{row['mode'].upper()} BUY {sig.side.upper()} {quantity} @ ≤{sig.limit_price:.3f} "
               f"mkt={sig.market_id} p_side={sig.p_side:.3f} edge={sig.edge:+.3f}")

        if not self.live:
            log.info(msg)
            self.db.log_signal(status="paper", **row)
            return True

        try:
            resp = orders.place_limit(
                self.client, sig.exchange_id, sig.side, "buy", quantity, sig.limit_price,
                self.tournament_id, ttl_seconds=self.order_ttl_seconds,
            )
        except SigAPIError as e:
            log.error("order rejected: %s | %s", e, msg)
            self.db.log_signal(status=f"error:{e.code}", response={"message": e.message, "details": e.details}, **row)
            return False
        log.info("%s → order %s traded=%s", msg, resp.get("orderId"), resp.get("quantityTraded"))
        self.db.log_signal(status="sent", order_id=str(resp.get("orderId")), response=resp, **row)
        return True
