"""Live fair values for the arb bot: Kalshi refreshed in a background thread, edges computed from
the SIG prices the bot already polls each cycle (no extra SIG reads), and a history of edges every
DIR_HISTORY_EVERY seconds so `sigbot convergence` can measure how fast SIG's gaps close.

Kalshi uses its own API and pacing, never the SIG read budget. Needs data/races.csv with names
and ratings (`sigbot races`) and data/kalshi_map.csv (`sigbot kalshi`).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from .config import Settings
from .data.db import DB
from .data.external import kalshi as ks
from .data.external.races import Race, load_races
from .models.fairvalue import Edge, edges, fair_value
from .trading.arb import Basket

log = logging.getLogger(__name__)
FULL_EVERY = 1800.0  # seconds between full per-event fetches (new candidates, new markets)
STORE_EVERY = 300.0  # seconds between Kalshi snapshots written to the database
OFFICE_OF = {"S": "senate", "G": "governor", "H": "house"}


class FairValueService:
    def __init__(self, s: Settings, kc: Optional[ks.KalshiClient] = None):
        self.s, self.cfg = s, s.dir
        self.races: Dict[str, Race] = load_races(s.races_path)
        self.mapping = ks.load_map(s.kalshi_map_path)
        self.kc = kc or ks.KalshiClient()
        self.kalshi: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.updated: float = 0.0  # wall time of the last refresh; nothing is trusted before one
        self.errors = 0
        self._tickers: List[str] = []  # every market seen in the last full fetch
        self._full_at = -1e9  # monotonic time of the last full (per-event) fetch
        self._stored_at = -1e9  # monotonic time quotes were last written to the database
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def available(cls, s: Settings) -> bool:
        races = load_races(s.races_path)
        return bool(races) and any(r.d_names for r in races.values()) and s.kalshi_map_path.exists()

    # ---- background refresh ----

    def refresh(self) -> int:
        """A full per-event fetch every FULL_EVERY (finds new candidates and markets); in between,
        one batch request for every known market."""
        if not self._tickers or time.monotonic() - self._full_at > FULL_EVERY:
            quotes = ks.fetch_quotes(self.kc, self.mapping, self.races)
            self._tickers = sorted({t for q in quotes for t in q.tickers.split(",") if t})
            self._full_at = time.monotonic()
        else:
            quotes = ks.fetch_quotes_batch(self.kc, self._tickers, self.mapping, self.races)
        snap: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for q in quotes:
            snap.setdefault(q.race, {})[q.party] = {"bid": q.bid, "ask": q.ask, "last": q.last,
                                                    "open_interest": q.open_interest, "volume_24h": q.volume_24h}
        self.kalshi, self.updated = snap, time.time()  # swapped in one assignment for the bot thread
        if time.monotonic() - self._stored_at >= STORE_EVERY:  # memory is always fresh; disk every 5 min
            DB(self.s.db_path).insert_kalshi(quotes)  # own connection: this runs on another thread
            self._stored_at = time.monotonic()
        return len(quotes)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.refresh()
                log.debug("kalshi: refreshed %d party quotes", n)
            except Exception as e:  # keep the last good snapshot
                self.errors += 1
                log.warning("kalshi refresh failed: %s", e)
            self._stop.wait(self.cfg.kalshi_refresh)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="kalshi")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def age(self) -> Optional[float]:
        return None if not self.updated else time.time() - self.updated

    def fresh(self) -> bool:
        a = self.age()
        return a is not None and a < self.cfg.kalshi_max_age

    # ---- edges ----

    def two_party(self, baskets: Iterable[Basket]) -> List[Basket]:
        out = []
        for b in baskets:
            if sorted(l.label for l in b.legs) != ["D", "R"] or b.key not in self.races:
                continue
            if OFFICE_OF.get(b.key.split("-")[0]) in self.cfg.offices:
                out.append(b)
        return out

    def edges(self, baskets: Iterable[Basket], sig: Dict[str, tuple]) -> List[Edge]:
        kalshi = self.kalshi if self.fresh() else {}  # never trade on an old Kalshi snapshot
        out: List[Edge] = []
        for b in self.two_party(baskets):
            fv = fair_value(self.races[b.key], kalshi.get(b.key), self.cfg)
            if fv is None:
                continue
            legs = {l.label: l.exchange_id for l in b.legs}
            out.extend(edges(fv, legs["D"], legs["R"], sig, self.cfg))
        return out
