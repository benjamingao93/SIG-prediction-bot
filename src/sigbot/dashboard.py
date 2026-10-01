"""Local dashboard: `sigbot dashboard`, then open http://localhost:8050.

Read-only. Bot activity comes from data/sig.db (the arb bot's per-cycle heartbeat and its order
log), which costs no API reads. Exchange data (balance, P&L, positions, fills) is fetched at
most once per 30 s however many tabs are open, on a small read budget of its own, so the
dashboard can't starve the bot of the account's 100 reads/minute.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import markets as mk
from .api.client import SigClient
from .config import Settings
from .data.db import DB

log = logging.getLogger(__name__)
HTML = Path(__file__).with_name("dashboard.html")
_RACE = re.compile(r"Will the (\w+) Party win the (.+?)\??$")


def race_name(title: str) -> str:
    m = _RACE.match(title or "")
    return m.group(2) if m else title


class ExchangeCache:
    def __init__(self, s: Settings, ttl: float = 30.0):
        self.s = s
        self.client = SigClient(s.api_key, s.base_url, read_budget=12, write_budget=1)
        self.ttl = ttl
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = {}
        self.fetched = 0.0
        self.titles: Dict[str, str] = {}  # exchange id → market title
        self.race_legs: Dict[str, List[str]] = {}  # race → exchange ids of its party markets
        self.titles_fetched = 0.0

    def get(self) -> Dict[str, Any]:
        with self.lock:
            if time.time() - self.fetched > self.ttl:
                self._refresh()
            return {**self.data, "age": round(time.time() - self.fetched)}

    def _refresh(self) -> None:
        slug = self.s.tournament_slug
        try:
            if time.time() - self.titles_fetched > 600:
                ms = mk.list_tournament_markets(self.client, slug)
                self.titles = {e.id: m.title for m in ms for e in m.exchanges}
                self.race_legs = {}
                for m in ms:
                    if m.is_binary and _RACE.match(m.title):
                        self.race_legs.setdefault(race_name(m.title), []).append(m.yes_exchange_id)
                self.titles_fetched = time.time()
            t = mk.get_tournament(self.client, slug)
            pnl = self.client.get(f"/tournaments/{slug}/portfolio/pnl", period="all")
            pos = self.client.get(f"/tournaments/{slug}/portfolio/positions").get("positions", [])
            fills = self.client.get(f"/tournaments/{slug}/portfolio/fills", limit=100).get("data", [])
            self.data = {
                "ok": True,
                "unhedged": unhedged(pos, self.race_legs, self.titles),
                "tournament": {"name": t.get("name"), "end": t.get("endDate"), "currency": t.get("currencyName")},
                "cash": t.get("myBalance"),
                "account_value": pnl.get("totalAccountValue"),
                "unrealized": pnl.get("unrealizedPnl"),
                "period_pnl": pnl.get("periodPnl"),
                "positions": [{
                    "market": p.get("marketTitle"), "race": race_name(p.get("marketTitle", "")),
                    "side": ((p.get("lots") or [{}])[0].get("side") or "yes").lower(),
                    "quantity": abs(float(p.get("quantity") or 0)), "avg_cost": p.get("avgCost"),
                    "price": p.get("currentPrice"), "unrealized": p.get("unrealizedPnl"),
                    "settled": p.get("settled"),
                } for p in pos],
                "fills": [{
                    "time": f.get("filledAt"), "order_id": f.get("orderId"),
                    "market": self.titles.get(str(f.get("exchangeId")), f"exchange {f.get('exchangeId')}"),
                    "side": f.get("side"), "quantity": abs(float(f.get("quantity") or 0)), "price": f.get("price"),
                } for f in fills],
            }
        except Exception as e:  # keep serving the last good data
            log.warning("exchange refresh failed: %s", e)
            self.data = {**self.data, "ok": False, "error": str(e)}
        self.fetched = time.time()


def unhedged(positions: List[Dict[str, Any]], race_legs: Dict[str, List[str]],
             titles: Dict[str, str]) -> List[Dict[str, Any]]:
    """Races where NO holdings differ across the party markets: not a complete basket, so the
    payout depends on who wins. Any shortfall is a leg the bot (or you) still needs to buy."""
    no_qty: Dict[str, float] = {}
    for p in positions:
        side = ((p.get("lots") or [{}])[0].get("side") or "").lower()
        if side == "no" and not p.get("settled"):
            no_qty[str(p.get("exchangeId"))] = abs(float(p.get("quantity") or 0))
    out = []
    for race, legs in race_legs.items():
        held = {ex: no_qty.get(ex, 0.0) for ex in legs}
        if not any(held.values()) or max(held.values()) - min(held.values()) < 1:
            continue
        top = max(held.values())
        out.append({"race": race, "legs": [
            {"market": titles.get(ex, ex), "no_held": q, "short": top - q} for ex, q in held.items()]})
    return out


def local_state(db: DB, s: Settings) -> Dict[str, Any]:
    status = db.get_status()
    if status:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["ts"])).total_seconds()
        status["age"] = round(age)
        status["running"] = age < max(20.0, 4 * status.get("poll_seconds", 4))
    orders: Dict[tuple, Dict[str, Any]] = {}
    for r in db.query("SELECT * FROM signals ORDER BY ts DESC LIMIT 400"):
        try:
            basket = json.loads(r["response"] or "{}").get("basket")
        except ValueError:
            basket = None
        if not basket:
            continue  # model-strategy signals, not arb
        key = (r["ts"], basket, r["mode"])
        o = orders.setdefault(key, {"time": r["ts"], "race": basket, "mode": r["mode"], "side": r["side"],
                                    "sets": r["quantity"], "status": r["status"], "prices": [],
                                    "profit_per_set": r["edge"]})
        o["prices"].append(r["price"])
    rows: List[Dict[str, Any]] = list(orders.values())[:60]
    for o in rows:
        o["planned_profit"] = (o["profit_per_set"] or 0) * (o["sets"] or 0)
    live = [o for o in orders.values() if o["mode"] == "live" and o["status"] == "sent"]
    return {
        "status": status,
        "repairs": db.get_repairs(),
        "kill_switch": s.kill_switch.exists(),
        "orders": rows,
        "live_trades": len(live),
        "live_planned_profit": sum((o["profit_per_set"] or 0) * (o["sets"] or 0) for o in live),
    }


def serve(s: Settings, port: int = 8050) -> None:
    db = DB(s.db_path)
    db_lock = threading.Lock()
    ex = ExchangeCache(s)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", HTML.read_bytes())
            elif self.path == "/api/state":
                with db_lock:
                    local = local_state(db, s)
                body = json.dumps({"now": datetime.now(timezone.utc).isoformat(), "local": local,
                                   "exchange": ex.get()}, default=str).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # quiet
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard on http://localhost:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
