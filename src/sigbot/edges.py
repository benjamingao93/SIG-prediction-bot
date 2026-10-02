"""`sigbot kalshi` and `sigbot edges`: where SIG disagrees with Kalshi and the forecasters.

No trading. `sync_kalshi` maps each two-party Senate/Governor race to its Kalshi event (cached in
data/kalshi_map.csv) and stores a snapshot of Kalshi's quotes. `compute_edges` turns those, plus
the ratings in races.csv, into a fair value per race and compares it with SIG's live prices;
the result is printed and saved for the dashboard.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .api import markets as mk
from .api.client import SigClient
from .config import Settings
from .data.db import DB
from .data.external import kalshi as ks
from .data.external.races import Race, load_races
from .models.fairvalue import Edge, edges, fair_value
from .trading import arb

log = logging.getLogger(__name__)
OFFICE_OF = {"S": "senate", "G": "governor", "H": "house"}


def _races(s: Settings) -> Dict[str, Race]:
    races = load_races(s.races_path)
    if not races or not any(r.d_names or r.r_names for r in races.values()):
        raise SystemExit(f"{s.races_path} has no nominee names or ratings yet: run `sigbot races` first.")
    return races


def two_party_baskets(s: Settings, client: SigClient, races: Dict[str, Race]) -> List[arb.Basket]:
    """SIG races in the configured offices with exactly a D and an R market."""
    ms = mk.list_tournament_markets(client, s.tournament_slug, status="open")
    out = []
    for b in arb.build_baskets(ms):
        if sorted(l.label for l in b.legs) != ["D", "R"] or b.key not in races:
            continue
        if OFFICE_OF.get(b.key.split("-")[0]) in s.dir.offices:
            out.append(b)
    return out


def sync_kalshi(s: Settings, client: SigClient, db: DB, rediscover: bool = False,
                kc: Optional[ks.KalshiClient] = None) -> Tuple[int, int]:
    """Refresh the race → Kalshi event map where needed, then store one snapshot of quotes."""
    races = _races(s)
    keys = [b.key for b in two_party_baskets(s, client, races)]
    kc = kc or ks.KalshiClient()
    existing = ks.load_map(s.kalshi_map_path)
    keep = {} if rediscover else {k: existing[k] for k in keys if k in existing}
    todo = [k for k in keys if k not in keep and not existing.get(k, {}).get("manual")]
    found = ks.discover(kc, todo) if todo else {}
    mapping = ks.merge_map({**keep, **found}, existing)
    ks.save_map(s.kalshi_map_path, mapping)
    quotes = ks.fetch_quotes(kc, {k: v for k, v in mapping.items() if k in keys}, races)
    db.insert_kalshi(quotes)
    return len(mapping), len(quotes)


def compute_edges(s: Settings, client: SigClient, db: DB) -> List[Edge]:
    races = _races(s)
    baskets = two_party_baskets(s, client, races)
    tid = mk.get_tournament(client, s.tournament_slug)["id"]
    ex_ids = [l.exchange_id for b in baskets for l in b.legs]
    sig = arb.quotes_from_prices(mk.bulk_prices(client, ex_ids, tid))
    kalshi = db.latest_kalshi()
    out: List[Edge] = []
    for b in baskets:
        fv = fair_value(races[b.key], kalshi.get(b.key), s.dir)
        if fv is None:
            continue
        legs = {l.label: l.exchange_id for l in b.legs}
        out.extend(edges(fv, legs["D"], legs["R"], sig, s.dir))
    return out


def kalshi_age_minutes(db: DB) -> Optional[float]:
    ts = [p["ts"] for parties in db.latest_kalshi().values() for p in parties.values()]
    if not ts:
        return None
    newest = max(datetime.fromisoformat(t) for t in ts)
    return (datetime.now(timezone.utc) - newest).total_seconds() / 60


def report(rows: List[Edge], top: int, show_all: bool, kalshi_age: Optional[float]) -> str:
    best = sorted(rows, key=lambda e: -e.edge)
    if not show_all:
        best = [e for e in best if e.edge > 0][:top]
    lines = [f"Kalshi snapshot: {'none' if kalshi_age is None else f'{kalshi_age:.0f} min old'}   "
             f"(edge = fair − price; a view is bought as NO on the other party)",
             f"{'race':7} {'view':4} {'price':>6} {'fair':>6} {'edge':>7} {'needs':>6}  {'SIG p(D)':>8} "
             f"{'Kalshi':>7} {'ratings':>7}  sources          flags"]
    for e in best:
        fv = e.fv
        flags = ("TRADE " if e.tradeable and e.agree else "") + ("" if e.agree else "sources-disagree ")
        fmt = lambda v: "   –" if v is None else f"{v:.3f}"
        lines.append(f"{e.race:7} {e.view:4} {e.price:6.3f} {e.fair:6.3f} {e.edge:+7.3f} {e.required:6.3f}  "
                     f"{fmt(e.sig_p_d):>8} {fmt(fv.kalshi_p_d):>7} {fmt(fv.rating_p_d):>7}  {fv.sources:16} {flags}")
    n_trade = sum(1 for e in rows if e.tradeable and e.agree)
    lines.append(f"\n{len({e.race for e in rows})} races with a fair value, {n_trade} views clear the bar with sources agreeing")
    return "\n".join(lines)
