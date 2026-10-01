"""Replay recorded book snapshots through model → signals → sizing; simulate fills at the
recorded levels; mark to outcome (settled) or last mid (open).

Caveat: data/inputs.csv holds your *current* views, so replaying it over old snapshots leaks
the future. Treat results as a sanity check of the trading logic, not proof of edge.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..api.models import Level, OrderBook
from ..config import RiskLimits
from ..data.db import DB
from ..data.external.inputs import RaceInputs
from ..models.base import MarketContext, ProbabilityModel
from ..trading import signals, sizing


@dataclass
class BacktestResult:
    trades: List[dict] = field(default_factory=list)
    cash: float = 0.0
    marked_value: float = 0.0

    @property
    def pnl(self) -> float:
        return self.cash + self.marked_value

    def summary(self) -> str:
        return (f"trades={len(self.trades)} cash_flow={self.cash:+.1f} "
                f"marked_value={self.marked_value:.1f} pnl={self.pnl:+.1f}")


def run_backtest(db: DB, model: ProbabilityModel, inputs: Dict[str, RaceInputs], limits: RiskLimits,
                 bankroll: float = 100_000, cooldown_s: float = 90.0) -> BacktestResult:
    res = BacktestResult()
    held: Dict[tuple, float] = defaultdict(float)  # (exchange, side) -> shares
    exposure: Dict[str, float] = defaultdict(float)
    last_trade: Dict[str, datetime] = {}
    last_mid: Dict[str, float] = {}
    market_of: Dict[str, str] = {}

    for r in db.query("SELECT * FROM book_snapshots ORDER BY ts"):
        ts = datetime.fromisoformat(r["ts"])
        book = OrderBook(r["exchange_id"], r["market_id"],
                         [Level(p, q) for p, q in json.loads(r["bids"])],
                         [Level(p, q) for p, q in json.loads(r["asks"])])
        market_of[book.exchange_id] = book.market_id
        if book.mid is not None:
            last_mid[book.exchange_id] = book.mid
        lt = last_trade.get(book.exchange_id)
        if lt is not None and (ts - lt).total_seconds() < cooldown_s:
            continue
        ctx = MarketContext(book.market_id, "", book.mid, inputs.get(book.market_id), None,
                            ts.replace(tzinfo=ts.tzinfo or timezone.utc))
        est = model.predict(ctx)
        if est is None:
            continue
        sig = signals.evaluate(est, book, limits.min_edge, limits.uncertainty_mult)
        if sig is None:
            continue
        room = min(limits.max_position - exposure[book.exchange_id], limits.max_order_cost)
        qty = min(sig.quantity, sizing.kelly_shares(sig.p_side, sig.avg_price, bankroll, limits.kelly_fraction),
                  int(max(0.0, room) // sig.limit_price))
        if qty <= 0:
            continue
        # fill against recorded levels
        levels = book.asks if sig.side == "yes" else book.no_asks()
        left, cost = qty, 0.0
        for lvl in levels:
            if lvl.price > sig.limit_price or left <= 0:
                break
            take = min(left, lvl.quantity)
            cost += take * lvl.price
            left -= take
        filled = qty - left
        res.cash -= cost
        held[(book.exchange_id, sig.side)] += filled
        exposure[book.exchange_id] += cost
        last_trade[book.exchange_id] = ts
        res.trades.append({"ts": r["ts"], "market_id": book.market_id, "side": sig.side,
                           "qty": filled, "avg": cost / filled if filled else None, "p": sig.p_side})

    for (ex_id, side), shares in held.items():
        outcome = db.outcome(market_of[ex_id])
        if outcome is not None and outcome.upper() in ("YES", "NO"):
            yes_value = 1.0 if outcome.upper() == "YES" else 0.0
        else:
            yes_value = last_mid.get(ex_id, 0.5)
        res.marked_value += shares * (yes_value if side == "yes" else 1 - yes_value)
    return res
