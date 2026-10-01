"""Main loop: collect → model → signal → size → risk → execute."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from .api import markets as mk
from .api import portfolio as pf
from .api.client import SigClient
from .config import Settings
from .data.collector import Collector
from .data.db import DB
from .data.external.inputs import RaceInputs, load_inputs
from .models.base import MarketContext, ProbabilityModel
from .models.ensemble import Ensemble
from .models.fundamentals import FundamentalsModel
from .trading import signals, sizing
from .trading.execution import Executor
from .trading.risk import RiskManager

log = logging.getLogger(__name__)


def days_until(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (t - now).total_seconds() / 86400)


def build_fundamentals(s: Settings) -> Optional[FundamentalsModel]:
    """None unless data/races.csv exists (`sigbot races`) and GENERIC_BALLOT_D is set."""
    if s.generic_ballot_d is None:
        log.warning("GENERIC_BALLOT_D not set: fundamentals model off")
        return None
    if not s.races_path.exists():
        log.warning("%s missing (run `sigbot races`): fundamentals model off", s.races_path)
        return None
    return FundamentalsModel.from_file(s.races_path, s.generic_ballot_d)


class Bot:
    def __init__(self, settings: Settings, client: SigClient, db: DB, live: bool,
                 model: Optional[ProbabilityModel] = None, sync_interval: float = 60.0):
        self.s = settings
        self.client = client
        self.db = db
        self.tournament = mk.get_tournament(client, settings.tournament_slug)
        tid = self.tournament["id"]
        self.collector = Collector(client, db, settings.tournament_slug, tid)
        self.fundamentals = build_fundamentals(settings)
        self.model = model or Ensemble(settings.market_weight, fundamentals=self.fundamentals)
        self.risk = RiskManager(settings.risk, settings.kill_switch)
        self.executor = Executor(client, db, tid, live=live)
        self.sync_interval = sync_interval
        self.inputs: Dict[str, RaceInputs] = {}
        self.cash = float(self.tournament.get("myBalance") or 0)
        self.equity: Optional[float] = None
        self._last_sync = 0.0

    def group_of(self) -> Dict[str, str]:
        """Your group from inputs.csv, else the office (house/senate/governor/chamber)."""
        out = {}
        for mid, m in self.collector.markets.items():
            g = self.fundamentals.group(m.title) if self.fundamentals else None
            if g:
                out[mid] = g
        out.update({mid: i.group for mid, i in self.inputs.items() if i.group != "ungrouped"})
        return out

    def sync(self) -> None:
        """Authoritative refresh of balance, positions and your inputs file."""
        self.inputs = load_inputs(self.s.inputs_path)
        if self.fundamentals and self.fundamentals.refresh():
            log.info("reloaded %s", self.s.races_path)
        t = mk.get_tournament(self.client, self.s.tournament_slug)
        self.cash = float(t.get("myBalance") or 0)
        positions = pf.positions(self.client, self.s.tournament_slug)
        self.risk.load_positions(positions, self.group_of())
        self.equity = self.cash + sum((p.current_price or p.avg_cost) * p.quantity
                                      for p in positions if not p.settled)
        if self.risk.starting_equity is None:
            self.risk.starting_equity = self.equity
        self._last_sync = time.monotonic()
        log.info("sync: cash=%.0f equity=%.0f positions=%d views=%d",
                 self.cash, self.equity, len(positions), sum(1 for i in self.inputs.values()
                                                             if i.p_poll or i.p_forecast))

    def evaluate(self, exchange_id: str) -> None:
        book = self.collector.books.get(exchange_id)
        market = next((m for m in self.collector.markets.values() if m.yes_exchange_id == exchange_id), None)
        if book is None or market is None:
            return
        now = datetime.now(timezone.utc)
        p_market = book.mid if book.mid is not None else market.exchanges[0].latest_price
        ctx = MarketContext(market.id, market.title, p_market, self.inputs.get(market.id),
                            days_until(market.settlement_date, now), now)
        est = self.model.predict(ctx)
        if est is None:
            return
        sig = signals.evaluate(est, book, self.s.risk.min_edge, self.s.risk.uncertainty_mult)
        if sig is None:
            return
        group = self.group_of().get(market.id, "ungrouped")
        decision = self.risk.check(exchange_id, market.id, group, self.cash, self.equity)
        bankroll = self.equity if self.equity is not None else self.cash
        qty = min(
            sig.quantity,
            sizing.kelly_shares(sig.p_side, sig.avg_price, bankroll, self.s.risk.kelly_fraction),
            int(math.floor(decision.allowed_cost / sig.limit_price)),
        )
        if qty <= 0:
            log.debug("skip %s: %s", market.title, decision.reason)
            return
        if self.executor.submit(sig, qty, est, p_market):
            self.risk.add(exchange_id, market.id, group, qty * sig.limit_price)

    def run(self, max_cycles: Optional[int] = None) -> None:
        self.collector.refresh_markets()
        self.sync()
        n = 0
        while max_cycles is None or n < max_cycles:
            t0 = time.monotonic()
            try:
                if time.monotonic() - self._last_sync > self.sync_interval:
                    self.sync()
                self.collector.step()
                for ex_id in list(self.collector.dirty):
                    self.evaluate(ex_id)
                self.collector.dirty.clear()
            except Exception:
                log.exception("cycle failed")
            n += 1
            time.sleep(max(0.0, self.collector.price_interval - (time.monotonic() - t0)))
