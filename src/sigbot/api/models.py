"""Typed views of API payloads.

Order books are YES-normalized: bids/asks are YES prices. Buying NO at n is the same
as selling YES at 1-n, so NO is bought by hitting YES *bids*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


@dataclass
class OrderBook:
    exchange_id: str
    market_id: str
    bids: List[Level]  # YES bids, descending
    asks: List[Level]  # YES asks, ascending
    as_of_seq: Optional[int] = None

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "OrderBook":
        as_of = d.get("asOf") or {}
        return cls(
            exchange_id=str(d["exchangeId"]),
            market_id=str(d.get("marketId", "")),
            bids=sorted((Level(float(b["price"]), float(b["quantity"])) for b in d.get("bids", [])),
                        key=lambda l: -l.price),
            asks=sorted((Level(float(a["price"]), float(a["quantity"])) for a in d.get("asks", [])),
                        key=lambda l: l.price),
            as_of_seq=as_of.get("sequence") if isinstance(as_of, dict) else None,
        )

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def no_asks(self) -> List[Level]:
        """Prices at which NO can be bought, ascending (derived from YES bids)."""
        return [Level(round(1 - b.price, 6), b.quantity) for b in self.bids]


@dataclass
class Exchange:
    id: str
    option: Optional[str]
    latest_price: Optional[float]


@dataclass
class Market:
    id: str
    title: str
    status: str
    settlement_date: Optional[str]
    settled_with: Optional[str]
    categories: List[str]
    is_multi_outcome: bool
    exchanges: List[Exchange] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Market":
        return cls(
            id=str(d["id"]),
            title=d.get("title", ""),
            status=d.get("status", ""),
            settlement_date=d.get("settlementDate"),
            settled_with=d.get("settledWith"),
            categories=list(d.get("categories") or []),
            is_multi_outcome=bool(d.get("isMultiOutcome")),
            exchanges=[
                Exchange(str(e["id"]), e.get("option"), e.get("latestPrice"))
                for e in d.get("exchanges") or []
            ],
        )

    @property
    def is_binary(self) -> bool:
        return not self.is_multi_outcome and len(self.exchanges) == 1

    @property
    def yes_exchange_id(self) -> Optional[str]:
        return self.exchanges[0].id if self.is_binary else None


@dataclass
class Position:
    exchange_id: str
    market_id: str
    title: str
    side: str  # "yes" | "no"
    quantity: float
    avg_cost: float
    cost_basis: float
    current_price: Optional[float]
    unrealized_pnl: float
    settled: bool

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Position":
        lots = d.get("lots") or []
        return cls(
            exchange_id=str(d["exchangeId"]),
            market_id=str(d["marketId"]),
            title=d.get("marketTitle", ""),
            side=(lots[0].get("side") or "yes").lower() if lots else "yes",
            quantity=float(d.get("quantity", 0)),
            avg_cost=float(d.get("avgCost", 0)),
            cost_basis=float(d.get("costBasis", 0)),
            current_price=d.get("currentPrice"),
            unrealized_pnl=float(d.get("unrealizedPnl", 0)),
            settled=bool(d.get("settled")),
        )
