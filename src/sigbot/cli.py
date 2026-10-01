"""sigbot command line.

  sigbot check                 verify key, scopes, tournament; print balance
  sigbot markets               list tournament markets (+ --inputs to seed data/inputs.csv)
  sigbot races                 rebuild data/races.csv (PVI, incumbents) from Wikipedia
  sigbot fundamentals          fundamentals model vs market price, biggest gaps first
  sigbot collect [--realtime]  stage 1: record prices/books, no trading
  sigbot arb                   arbitrage: paper (logs baskets it would buy, sends nothing)
  sigbot arb --live            arbitrage with real orders (also requires MODE=live in .env)
  sigbot hedge RACE --max-price P   have the arb bot finish hedging an uneven race (NO side)
  sigbot dashboard             local dashboard at http://localhost:8050 (read-only)
  sigbot run                   model strategy, paper trading (logs signals, sends nothing)
  sigbot run --live            stage 4: real orders (also requires MODE=live in .env)
  sigbot backtest              stage 2: replay recorded books through the strategy
  sigbot report                signal log + calibration of recorded prices vs outcomes
  sigbot cancel-all            cancel every open order in the tournament
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .api import markets as mk
from .api import orders as od
from .api import portfolio as pf
from .api.client import SigAPIError, SigClient
from .config import Settings, load_settings
from .data.db import DB


def _client(s: Settings) -> SigClient:
    return SigClient(s.api_key, s.base_url, read_budget=s.read_budget, write_budget=s.write_budget)


def _require_slug(s: Settings) -> str:
    if not s.tournament_slug:
        raise SystemExit("Set SIG_TOURNAMENT_SLUG in .env (run `sigbot check` to list your tournaments).")
    return s.tournament_slug


def cmd_check(s: Settings, a) -> None:
    c = _client(s)
    acct = pf.account(c)
    print(f"key OK — user={acct.get('username')} global_balance={acct.get('balance')}")
    ts = mk.list_tournaments(c)
    print("your tournaments:")
    for t in ts:
        print(f"  slug={t.get('slug')}  name={t.get('name')}  status={t.get('status')}")
    if s.tournament_slug:
        t = mk.get_tournament(c, s.tournament_slug)
        print(f"\nusing {t['name']} (id={t['id']}) {t['startDate']} → {t['endDate']}")
        print(f"  my balance: {t.get('myBalance')} {t.get('currencyName')}  "
              f"markets={t.get('marketCount')}  pending_enrolment={t.get('isPendingEnrolment')}")
    else:
        print("\nSIG_TOURNAMENT_SLUG not set — pick a slug above and add it to .env")


def cmd_markets(s: Settings, a) -> None:
    from .data.external.inputs import write_template
    c = _client(s)
    ms = mk.list_tournament_markets(c, _require_slug(s), status="open")
    for m in ms:
        ex = m.exchanges[0] if m.exchanges else None
        print(f"{m.id:>8}  ex={ex.id if ex else '-':>8}  last={ex.latest_price if ex else None}  "
              f"{'' if m.is_binary else '[multi] '}{m.title}")
    if a.inputs:
        n = write_template(s.inputs_path, [m for m in ms if m.is_binary])
        print(f"\nadded {n} rows to {s.inputs_path} — fill in p_poll / p_forecast / group")


def cmd_races(s: Settings, a) -> None:
    from dataclasses import replace

    from .data.external import races as rc
    from .models.fundamentals import parse_title
    pages = {name: rc.fetch_page(name) for name in rc.PAGES}
    fresh = rc.parse_house(pages["house"]) + rc.parse_senate(pages["senate"]) + rc.parse_governor(pages["governor"])
    # An Independent market means a real three-way race the model can't price: skip it.
    ms = mk.list_tournament_markets(_client(s), _require_slug(s), status="open")
    indep = {p[0] for p in (parse_title(m.title) for m in ms) if p and p[1] == "I"}
    fresh = [replace(r, skip=True, notes="independent in race") if r.race in indep else r for r in fresh]
    out = rc.merge_user_columns(fresh, rc.load_races(s.races_path))
    rc.write_races(s.races_path, out)
    counts = {o: sum(1 for r in out if r.office == o) for o in ("house", "senate", "governor")}
    print(f"wrote {s.races_path}: {counts}")
    gb = rc.parse_generic_ballot(pages["house"])
    if gb is not None:
        print(f"generic ballot average on Wikipedia: D{gb:+.1f}   (.env GENERIC_BALLOT_D={s.generic_ballot_d})")


def cmd_fundamentals(s: Settings, a) -> None:
    from .bot import build_fundamentals
    fm = build_fundamentals(s)
    if fm is None:
        raise SystemExit("needs data/races.csv (`sigbot races`) and GENERIC_BALLOT_D in .env")
    rows = []
    for m in mk.list_tournament_markets(_client(s), _require_slug(s), status="open"):
        p = fm.p_yes(m.title)
        last = m.exchanges[0].latest_price if m.exchanges else None
        if p is not None and last is not None:
            rows.append((p - last, p, last, m.title))
    rows.sort(key=lambda r: -abs(r[0]))
    print(f"env D{fm.env:+.1f}   model  market   gap")
    for gap, p, last, title in rows[:a.top]:
        print(f"  {p:6.3f}  {last:6.3f}  {gap:+6.3f}  {title}")


def cmd_collect(s: Settings, a) -> None:
    from .data.collector import Collector
    c, db = _client(s), DB(s.db_path)
    slug = _require_slug(s)
    tid = mk.get_tournament(c, slug)["id"]
    col = Collector(c, db, slug, tid)
    if a.realtime:
        from .api.realtime import RealtimeBooks
        col.refresh_markets()
        rt = RealtimeBooks(c, tid, list(col.markets), on_book=col.set_book,
                           on_resync=lambda mid: col.markets.get(mid) and col.fetch_book(col.markets[mid].yes_exchange_id))
        asyncio.run(rt.run())
    else:
        col.run(max_cycles=a.cycles)


def cmd_run(s: Settings, a) -> None:
    from .bot import Bot
    live = a.live
    if live and s.mode != "live":
        raise SystemExit("--live also requires MODE=live in .env. Refusing to send orders.")
    _require_slug(s)
    if live:
        print("LIVE TRADING — create a file named", s.kill_switch, "to stop new orders.")
    Bot(s, _client(s), DB(s.db_path), live=live).run(max_cycles=a.cycles)


def cmd_arb(s: Settings, a) -> None:
    from .arb_bot import ArbBot
    live = a.live
    if live and s.mode != "live":
        raise SystemExit("--live also requires MODE=live in .env. Refusing to send orders.")
    _require_slug(s)
    if live:
        print("LIVE ARBITRAGE — create a file named", s.kill_switch, "to stop new orders.")
    ArbBot(s, _client(s), DB(s.db_path), live=live).run(max_cycles=a.cycles)


def cmd_hedge(s: Settings, a) -> None:
    from .models.fundamentals import parse_title
    if not a.cancel and a.max_price is None:
        raise SystemExit("--max-price is required: the most you'll pay per share to finish the hedge")
    db = DB(s.db_path)
    c = _client(s)
    ms = mk.list_tournament_markets(c, _require_slug(s), status="open")
    legs = {}  # race key → [(exchange id, title)]
    for m in ms:
        p = parse_title(m.title)
        if p and m.is_binary:
            legs.setdefault(p[0], []).append((m.yes_exchange_id, m.title))
    key = a.race.upper()
    if key not in legs:  # accept "Alaska Senate" as well as "S-AK"
        key = next((k for k, ls in legs.items() if a.race.lower() in ls[0][1].lower()), key)
    if key not in legs:
        raise SystemExit(f"no race matches {a.race!r}; use a key like S-AK or a name like 'Alaska Senate'")
    if a.cancel:
        db.delete_repair(key)
        print(f"removed the pending repair for {key}")
        return
    held = {str(p.exchange_id): abs(p.quantity) for p in pf.positions(c, s.tournament_slug)
            if p.side == "no" and not p.settled}
    q = {ex: held.get(ex, 0.0) for ex, _ in legs[key]}
    top = max(q.values())
    short = [{"exchange_id": ex, "title": t, "short": top - q[ex], "cap": a.max_price}
             for ex, t in legs[key] if top - q[ex] >= 1]
    for ex, t in legs[key]:
        print(f"  {t}: {q[ex]:.0f} NO held")
    if not short:
        print(f"{key} is already even: nothing to hedge")
        return
    db.save_repair(key, {"side": "no", "legs": short, "source": "manual"})
    for l in short:
        print(f"→ bot will buy up to {l['short']:.0f} NO on {l['title']} at ≤{a.max_price:.3f}")
    print("The running `sigbot arb --live` picks this up next cycle (not while KILL exists).")


def cmd_dashboard(s: Settings, a) -> None:
    from .dashboard import serve
    _require_slug(s)
    serve(s, port=a.port)


def cmd_backtest(s: Settings, a) -> None:
    from .backtest.engine import run_backtest
    from .bot import build_fundamentals
    from .data.external.inputs import load_inputs
    from .models.ensemble import Ensemble
    model = Ensemble(s.market_weight, fundamentals=build_fundamentals(s))
    res = run_backtest(DB(s.db_path), model, load_inputs(s.inputs_path), s.risk, bankroll=a.bankroll)
    for t in res.trades[-20:]:
        print(t)
    print(res.summary())


def cmd_report(s: Settings, a) -> None:
    from .models.calibration import brier, reliability_table
    db = DB(s.db_path)
    print("signals by mode/status:")
    for r in db.query("SELECT mode, status, COUNT(*) n, SUM(quantity*price) cost FROM signals GROUP BY 1,2"):
        print(f"  {r['mode']:6} {r['status']:20} n={r['n']:<5} cost={r['cost'] or 0:.0f}")
    pairs = []
    for r in db.query("""SELECT p.latest, o.settled_with FROM price_snapshots p
                         JOIN outcomes o ON o.market_id = p.market_id WHERE p.latest IS NOT NULL"""):
        sw = (r["settled_with"] or "").upper()
        if sw in ("YES", "NO"):
            pairs.append((r["latest"], 1 if sw == "YES" else 0))
    if pairs:
        print(f"\ncalibration over {len(pairs)} snapshots (brier={brier(pairs):.4f}):")
        for b in reliability_table(pairs):
            print(f"  {b['lo']:.1f}-{b['hi']:.1f}  n={b['n']:<6} price={b['mean_p']:.3f}  actual={b['freq_yes']:.3f}")
    else:
        print("\nno settled markets yet — calibration available after resolutions")


def cmd_cancel_all(s: Settings, a) -> None:
    c = _client(s)
    tid = mk.get_tournament(c, _require_slug(s))["id"]
    print("cancelled", od.cancel_all(c, tid))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="sigbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    m = sub.add_parser("markets"); m.add_argument("--inputs", action="store_true"); m.set_defaults(fn=cmd_markets)
    sub.add_parser("races").set_defaults(fn=cmd_races)
    f = sub.add_parser("fundamentals"); f.add_argument("--top", type=int, default=60); f.set_defaults(fn=cmd_fundamentals)
    c = sub.add_parser("collect"); c.add_argument("--realtime", action="store_true")
    c.add_argument("--cycles", type=int); c.set_defaults(fn=cmd_collect)
    r = sub.add_parser("run"); r.add_argument("--live", action="store_true")
    r.add_argument("--cycles", type=int); r.set_defaults(fn=cmd_run)
    ar = sub.add_parser("arb"); ar.add_argument("--live", action="store_true")
    ar.add_argument("--cycles", type=int); ar.set_defaults(fn=cmd_arb)
    h = sub.add_parser("hedge"); h.add_argument("race"); h.add_argument("--max-price", type=float)
    h.add_argument("--cancel", action="store_true"); h.set_defaults(fn=cmd_hedge)
    d = sub.add_parser("dashboard"); d.add_argument("--port", type=int, default=8050); d.set_defaults(fn=cmd_dashboard)
    b = sub.add_parser("backtest"); b.add_argument("--bankroll", type=float, default=100_000); b.set_defaults(fn=cmd_backtest)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("cancel-all").set_defaults(fn=cmd_cancel_all)
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = load_settings(a.env, require_key=a.cmd not in ("backtest", "report"))
    try:
        a.fn(s, a)
    except SigAPIError as e:
        hint = {
            "TERMS_NOT_ACKNOWLEDGED": "accept the current Terms on the site",
            "INSUFFICIENT_SCOPES": "recreate the key with read + trade scopes",
            "RESIDENCE_UPDATE_REQUIRED": "update your Predictions Cup registration on the site",
            "FORBIDDEN": "confirm your email / check tournament membership",
        }.get(e.code, "")
        sys.exit(f"API error: {e}" + (f"\n→ {hint}" if hint else ""))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
