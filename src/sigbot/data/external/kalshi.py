"""Kalshi prices for the same races: a real-money market to measure SIG's prices against.

Public market data, no account needed (https://api.elections.kalshi.com/trade-api/v2). Kalshi
rate-limits fast sweeps, so requests are paced and 429s are retried with backoff.

Kalshi has several series per race: the general election (`SENATE{ST}`, `KXSENATE{ST}`,
`SENATEFLS` for Florida's special, `GOVPARTY{ST}`, `KXGOV{ST}`, `KXGOVPARTY{ST}`) plus primary
and nominee series (names ending in D/R/NOM...) and the odd junk series. `discover` keeps only the
general-election patterns and, per race, picks the open event with the most open interest. The
choice is saved in data/kalshi_map.csv; rows you mark `manual=1` are never overwritten.

Kalshi lists a race either by party ("Republican party") or by candidate ("Jeff Merkley").
`party_of` maps both to D/R using the nominee names from races.csv, and anything else to "O"
(an independent, or a candidate who isn't the nominee). Several markets for one party (e.g. two
Republicans on Alaska's top-four ballot) are summed: mutually exclusive outcomes.
"""
from __future__ import annotations

import csv
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from .races import Race

log = logging.getLogger(__name__)
BASE = "https://api.elections.kalshi.com/trade-api/v2"
MAP_COLUMNS = ["race", "event_ticker", "series", "open_interest", "manual"]


@dataclass(frozen=True)
class KalshiQuote:
    race: str
    party: str  # "D" | "R" | "O"
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    open_interest: float
    volume_24h: float
    tickers: str  # the Kalshi market(s) summed into this quote

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class KalshiClient:
    def __init__(self, min_interval: float = 0.6, timeout: float = 20.0, max_retries: int = 5,
                 transport: Optional[httpx.BaseTransport] = None, sleep=time.sleep):
        self._http = httpx.Client(base_url=BASE, timeout=timeout, transport=transport,
                                  headers={"Accept": "application/json", "User-Agent": "sigbot/0.1"})
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleep
        self._last = 0.0

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            r = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
            if r.status_code == 429 and attempt < self.max_retries:
                self._sleep(float(r.headers.get("Retry-After") or 5 * (attempt + 1)))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")


# ---- discovery ----

def series_candidates(series_tickers: Iterable[str], race_key: str) -> List[str]:
    """General-election series that can belong to a SIG race key like S-TX or G-GA."""
    office, st = race_key.split("-", 1)
    if office == "S":
        pat = re.compile(rf"(?:KX)?SENATE{st}S?")
    elif office == "G":
        pat = re.compile(rf"(?:KX)?GOV(?:PARTY)?{st}")
    else:
        return []
    return sorted(t for t in series_tickers if pat.fullmatch(t))


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _event_score(event: Dict[str, Any]) -> float:
    oi = sum(_num(m.get("open_interest_fp")) or 0.0 for m in event.get("markets") or [])
    text = f"{event.get('title', '')} {event.get('sub_title', '')}"
    return oi if "2026" in text else oi * 0.01  # strongly prefer this cycle's event


def discover(client: KalshiClient, race_keys: Iterable[str],
             series_tickers: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """race → {event_ticker, series, open_interest} for the best open general-election event."""
    if series_tickers is None:
        series_tickers = [s["ticker"] for s in client.get("/series", category="Elections").get("series") or []]
    out: Dict[str, Dict[str, Any]] = {}
    for key in race_keys:
        best: Optional[Tuple[float, Dict[str, Any], str]] = None
        for t in series_candidates(series_tickers, key):
            events = client.get("/events", series_ticker=t, status="open",
                                with_nested_markets="true").get("events") or []
            for e in events:
                score = _event_score(e)
                if best is None or score > best[0]:
                    best = (score, e, t)
        if best is not None and best[0] > 0:
            out[key] = {"event_ticker": best[1]["event_ticker"], "series": best[2], "open_interest": round(best[0], 2)}
        else:
            log.info("kalshi: no open general-election event for %s", key)
    return out


def load_map(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {r["race"]: {**r, "manual": (r.get("manual") or "").strip() in ("1", "true", "yes")}
                for r in csv.DictReader(f) if r.get("race")}


def save_map(path: Path, mapping: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(MAP_COLUMNS)
        for race in sorted(mapping):
            m = mapping[race]
            w.writerow([race, m["event_ticker"], m.get("series", ""), m.get("open_interest", ""),
                        "1" if m.get("manual") else ""])


def merge_map(fresh: Dict[str, Dict[str, Any]], existing: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fresh discoveries, except races you pinned by hand (manual=1)."""
    out = dict(fresh)
    out.update({r: m for r, m in existing.items() if m.get("manual")})
    return out


# ---- matching markets to parties ----

def _norm(s: str) -> str:
    """'Jeffrey A. Merkley' → 'jeffrey a merkley': ASCII, lower case, letters and single spaces."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z]", " ", s).split())


def _same_person(a: str, b: str) -> bool:
    """'Jeff Merkley' ~ 'Jeffrey A. Merkley': same last name and first initial, or identical."""
    na, nb = _norm(a).split(), _norm(b).split()
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na[-1] == nb[-1] and na[0][0] == nb[0][0]


def party_of(label: str, race: Race) -> str:
    lab = _norm(label)
    if re.search(r"\bdemocrat", lab):
        return "D"
    if re.search(r"\brepublican", lab) or re.search(r"\bgop\b", lab):
        return "R"
    for party, names in (("D", race.d_names), ("R", race.r_names)):
        if any(_same_person(label, n) for n in names.split(";") if n.strip()):
            return party
    return "O"


def quotes_for_event(race: Race, event: Dict[str, Any]) -> List[KalshiQuote]:
    """One quote per party, summing the party's candidates (mutually exclusive outcomes).
    A party's bid/ask is only known when every one of its markets has one."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for m in event.get("markets") or []:
        if m.get("status") not in (None, "active", "open"):
            continue
        label = m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or ""
        groups.setdefault(party_of(label, race), []).append(m)
    out = []
    for party, ms in groups.items():
        bids = [_num(m.get("yes_bid_dollars")) for m in ms]
        asks = [_num(m.get("yes_ask_dollars")) for m in ms]
        lasts = [_num(m.get("last_price_dollars")) for m in ms]
        out.append(KalshiQuote(
            race.race, party,
            bid=sum(bids) if all(b is not None for b in bids) else None,
            ask=min(1.0, sum(asks)) if all(a is not None and a > 0 for a in asks) else None,
            last=sum(lasts) if all(x is not None for x in lasts) else None,
            open_interest=sum(_num(m.get("open_interest_fp")) or 0.0 for m in ms),
            volume_24h=sum(_num(m.get("volume_24h_fp")) or 0.0 for m in ms),
            tickers=",".join(m.get("ticker", "") for m in ms)))
    return out


def fetch_quotes(client: KalshiClient, mapping: Dict[str, Dict[str, Any]],
                 races: Dict[str, Race]) -> List[KalshiQuote]:
    out: List[KalshiQuote] = []
    for race_key, m in sorted(mapping.items()):
        race = races.get(race_key)
        if race is None:
            continue
        try:
            ev = client.get(f"/events/{m['event_ticker']}", with_nested_markets="true")
        except httpx.HTTPError as e:
            log.warning("kalshi %s (%s): %s", race_key, m["event_ticker"], e)
            continue
        event = ev.get("event") or ev
        if "markets" not in event and ev.get("markets"):
            event = {**event, "markets": ev["markets"]}
        out.extend(quotes_for_event(race, event))
    return out
