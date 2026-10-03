import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigbot import arb_bot, directional
from sigbot.api.models import Exchange, Level, Market, OrderBook
from sigbot.config import ArbConfig, DirectionalConfig
from sigbot.data.db import DB
from sigbot.data.external.races import Race
from sigbot.directional import Director
from sigbot.models.fairvalue import edges, fair_value
from sigbot.trading import arb


def mkt(mid, party, race="Rhode Island Senate"):
    return Market(mid, f"Will the {party} Party win the {race}?", "open", None, None, [], False,
                  [Exchange(f"e{mid}", "YES", None)])


BASKETS = arb.build_baskets([mkt("1", "Democratic"), mkt("2", "Republican"),
                             mkt("3", "Democratic", "Maine Senate"), mkt("4", "Republican", "Maine Senate")])


def kq(d, r):
    return {"D": {"bid": d[0], "ask": d[1], "open_interest": 9000}, "R": {"bid": r[0], "ask": r[1], "open_interest": 9000}}


def make_edges(cfg, ri_kalshi=((.985, .995), (.006, .014)), sig=None):
    sig = sig or {"e1": (.93, .94), "e2": (.07, .08), "e3": (.55, .56), "e4": (.44, .45)}
    ri = fair_value(Race("S-RI", "senate", 8, rating_p_d=.97, rating_n=7, rating_spread=0), kq(*ri_kalshi), cfg)
    me = fair_value(Race("S-ME", "senate", 4, rating_p_d=.55, rating_n=10, rating_spread=.2), kq((.55, .57), (.43, .45)), cfg)
    return edges(ri, "e1", "e2", sig, cfg) + edges(me, "e3", "e4", sig, cfg), sig


class FakeOrders:
    def __init__(self):
        self.calls = []

    def place_limit(self, client, ex, side, action, qty, price, tid, ttl_seconds=None):
        self.calls.append((ex, side, action, qty, round(price, 3)))
        return {"orderId": len(self.calls), "open": False, "quantityTraded": qty, "totalCost": qty * price}


def make(tmp_path, live=False, books=None, dcfg=None, monkeypatch=None, fresh=True):
    dcfg = dcfg or DirectionalConfig(directional=True)
    es, sig = make_edges(dcfg)
    books = books or {"e2": OrderBook("e2", "2", [Level(.07, 5000)], [Level(.08, 5000)]),
                      "e4": OrderBook("e4", "4", [Level(.44, 5000)], [Level(.45, 5000)])}
    bot = SimpleNamespace(
        s=SimpleNamespace(dir=dcfg, kill_switch=Path(tmp_path / "KILL")), cfg=ArbConfig(), live=live,
        db=DB(tmp_path / "t.db"), _quotes=dict(sig), _edges=es, repairs={}, baskets=BASKETS, cash=1e6, tid="t",
        fair=SimpleNamespace(fresh=lambda: fresh), client=None)
    bot._books = lambda exs: ({ex: books[ex] for ex in exs}, time.monotonic())
    bot._stale = lambda read_at, what: False
    bot._can_read = lambda n: True
    bot._settle_leg = lambda r: r["quantityTraded"]
    return Director(bot), bot


def test_enters_best_return_on_capital_as_no_on_the_other_party(tmp_path):
    d, bot = make(tmp_path)
    d.step()
    held = d.positions()
    ri = held["S-RI"]
    assert ri["view"] == "D" and ri["exchange_id"] == "e2"  # Democrat cheap → NO on the Republican
    assert ri["entry_price"] == pytest.approx(.93)
    # ¼-Kelly on 20,000 at p≈.99/q=.93 is ~4,600 shares; the 2,000 race cap allows 2,150 at .93.
    assert ri["qty"] == 2150 and ri["cost"] <= 2000


def test_one_position_per_race_and_no_reentry(tmp_path):
    d, bot = make(tmp_path)
    d.step()
    n = len(d.positions())
    d.step()
    assert len(d.positions()) == n


def test_depth_cushion_and_net_direction_cap(tmp_path):
    books = {"e2": OrderBook("e2", "2", [Level(.07, 100)], [Level(.08, 100)]),
             "e4": OrderBook("e4", "4", [Level(.44, 100)], [Level(.45, 100)])}
    d, bot = make(tmp_path, books=books)
    d.step()
    assert d.positions()["S-RI"]["qty"] == 50  # half of the 100 shown
    d2, bot2 = make(tmp_path / "b", dcfg=DirectionalConfig(directional=True, max_net=465))
    d2.step()
    assert d2.positions()["S-RI"]["cost"] <= 465 + 1


def test_loss_stop_blocks_new_entries(tmp_path):
    d, bot = make(tmp_path)
    bot.db.add_dir_realized("paper", -2500)
    d.step()
    assert d.positions() == {}


def test_nothing_happens_on_stale_kalshi(tmp_path):
    d, bot = make(tmp_path, fresh=False)
    d.step()
    assert d.positions() == {}


def test_take_half_then_rest_then_stop(tmp_path):
    d, bot = make(tmp_path)
    d.step()
    p = d.positions()["S-RI"]
    fair = next(e.fair for e in bot._edges if e.race == "S-RI" and e.view == "D")
    # SIG closes half the gap: the NO on R can now be sold at 1 − YES ask.
    half_px = p["entry_price"] + 0.6 * (fair - p["entry_price"])
    bot._quotes["e2"] = (0.0, round(1 - half_px, 3))
    d._manage(p)
    after = d.positions()["S-RI"]
    assert after["halved"] == 1 and after["qty"] == p["qty"] - p["qty"] // 2
    bot._quotes["e2"] = (0.0, round(1 - (fair - 0.002), 3))  # within the exit band of fair
    d._manage(d.positions()["S-RI"])
    assert "S-RI" not in d.positions() and bot.db.dir_realized("paper") > 0


def test_stop_cuts_when_fair_value_falls_below_entry(tmp_path):
    d, bot = make(tmp_path)
    d.step()
    p = d.positions()["S-RI"]
    # Kalshi turns: the Democrat is now a coin flip.
    es, _ = make_edges(d.cfg, ri_kalshi=((.49, .51), (.49, .51)))
    bot._edges = es
    bot._quotes["e2"] = (0.0, 0.40)  # NO on R sells for .60
    d._manage(p)
    assert "S-RI" not in d.positions() and bot.db.dir_realized("paper") < 0


def test_live_orders_are_no_buys_and_sells(tmp_path, monkeypatch):
    fake = FakeOrders()
    monkeypatch.setattr(directional, "orders", fake)
    d, bot = make(tmp_path, live=True)
    d.step()
    assert fake.calls[0][:3] == ("e2", "no", "buy")
    assert d.budget == 5000 and d.positions()["S-RI"]["cost"] <= 500  # live starts small
    bot._quotes["e2"] = (0.0, 0.005)  # NO sells at .995: take profit
    d._manage(d.positions()["S-RI"])
    assert fake.calls[-1][:3] == ("e2", "no", "sell")


def test_baskets_exclude_the_directional_ledger(tmp_path, monkeypatch):
    from tests.test_arb import live_bot  # reuse the arb bot fixture
    bot, _, _ = live_bot(monkeypatch, tmp_path, [])
    bot.baskets = BASKETS
    pos = [SimpleNamespace(side="no", settled=False, exchange_id="e1", quantity=-100, avg_cost=.9, cost_basis=90.0),
           SimpleNamespace(side="no", settled=False, exchange_id="e2", quantity=-600, avg_cost=.5, cost_basis=400.0)]
    monkeypatch.setattr(arb_bot.pf, "positions", lambda c, slug: pos)
    bot.s.tournament_slug = "x"
    bot.client.reads = SimpleNamespace(available=100)
    bot.director = SimpleNamespace(live=True, ledger_qty=lambda: {"e2": 500}, ledger_cost=lambda: {"e2": 350.0})
    bot.refresh_positions(force=True)
    assert bot.held == {"S-RI": 100}  # 600 NO on e2 minus the 500 the directional trader owns
    assert bot.held_cost["S-RI"] == pytest.approx(.9 + 50.0 / 100)


def test_directional_paper_inside_a_live_bot(tmp_path, monkeypatch):
    fake = FakeOrders()
    monkeypatch.setattr(directional, "orders", fake)
    d, bot = make(tmp_path, live=True, dcfg=DirectionalConfig(directional=True, paper=True))
    d.step()
    assert d.mode == "paper" and fake.calls == [] and d.positions()  # simulated, nothing sent
    assert d.ledger_qty() == {}  # paper positions never touch basket detection


# ---- aggressive-mode safeguards ----

def test_live_entries_keep_the_cash_reserve(tmp_path, monkeypatch):
    fake = FakeOrders()
    monkeypatch.setattr(directional, "orders", fake)
    dcfg = DirectionalConfig(directional=True, live_budget=50_000, live_max_race=10_000, cash_reserve=2_000)
    d, bot = make(tmp_path, live=True, dcfg=dcfg)
    bot.cash = 2_500  # only 500 above the reserve
    d.step()
    spent = sum(p["cost"] for p in d.positions().values())
    assert 0 < spent <= 500 + 1


def test_min_order_skips_dust(tmp_path):
    books = {"e2": OrderBook("e2", "2", [Level(.07, 30)], [Level(.08, 30)]),
             "e4": OrderBook("e4", "4", [Level(.44, 30)], [Level(.45, 30)])}
    d, bot = make(tmp_path, books=books, dcfg=DirectionalConfig(directional=True, depth_fraction=0.5, min_order=25))
    d.step()  # 15 shares × .93 = 14 SUSQies: below the minimum
    assert "S-RI" not in d.positions()


def test_recycle_sells_the_weakest_old_position_for_a_better_gap(tmp_path):
    d, bot = make(tmp_path, dcfg=DirectionalConfig(directional=True, budget=1_000, max_race=1_000))
    old = {"mode": "paper", "race": "S-ME", "view": "D", "exchange_id": "e4", "market_id": "4", "title": "",
           "qty": 1700, "cost": 977.5, "entry_price": .575, "fair_entry": .593, "halved": 0, "realized": 0.0,
           "opened": "2026-10-01T00:00:00+00:00"}
    bot.db.save_dir_position(old)
    # NO on R sells at .57: below its .575 entry (no take-profit), fair ≈ .557 is above the stop
    # (.545), and holding has nothing left to earn, versus RI's ~6%.
    bot._quotes["e4"] = (.42, .43)
    d.step()
    held = d.positions()
    # Only what RI's buy can use (¼-Kelly on a 1,000 budget: ~200) is sold, not the whole position.
    ri_cost = held["S-RI"]["cost"]
    sold = 1700 - held["S-ME"]["qty"]
    assert 0 < ri_cost < 300 and sold == pytest.approx((ri_cost - 22.5) / .57, abs=2)


def test_recycle_sells_only_what_the_new_buy_can_take_and_spends_it_there(tmp_path, monkeypatch):
    fake = FakeOrders()
    monkeypatch.setattr(directional, "orders", fake)
    # RI's NO ask has only 400 shares: that's all the new buy can use.
    books = {"e2": OrderBook("e2", "2", [Level(.07, 400)], [Level(.08, 400)]),
             "e4": OrderBook("e4", "4", [Level(.44, 5000)], [Level(.45, 5000)])}
    dcfg = DirectionalConfig(directional=True, live_budget=50_000, live_max_race=10_000, cash_reserve=2_000,
                             depth_fraction=1.0)
    d, bot = make(tmp_path, live=True, dcfg=dcfg, books=books)
    bot.cash = 2_000  # nothing above the reserve
    old = {"mode": "live", "race": "S-ME", "view": "D", "exchange_id": "e4", "market_id": "4", "title": "",
           "qty": 8000, "cost": 4600.0, "entry_price": .575, "fair_entry": .593, "halved": 0, "realized": 0.0,
           "opened": "2026-10-01T00:00:00+00:00"}
    bot.db.save_dir_position(old)
    bot._quotes["e4"] = (.42, .43)
    d.step()
    sells = [c for c in fake.calls if c[2] == "sell"]
    buys = [c for c in fake.calls if c[2] == "buy"]
    assert sells == [("e4", "no", "sell", 653, .57)]  # 400 × .93 = 372 needed → 653 shares at .57
    assert buys == [("e2", "no", "buy", 400, .93)]  # the freed cash went to RI, this cycle
    assert d.positions()["S-ME"]["qty"] == 8000 - 653


def test_a_close_race_gap_counts_for_less_than_a_favourites(tmp_path):
    d, bot = make(tmp_path)
    ri = d._edge("S-RI", "D")
    # Same raw return on capital, but the bar (min edge + uncertainty) is taken off first.
    loose = ri.__class__(**{**ri.__dict__, "required": .10})
    assert d._spare_return(loose, .5) < d._spare_return(ri, .5)


def test_recycle_leaves_young_positions_alone(tmp_path):
    d, bot = make(tmp_path, dcfg=DirectionalConfig(directional=True, budget=1_000, max_race=1_000))
    young = {"mode": "paper", "race": "S-ME", "view": "D", "exchange_id": "e4", "market_id": "4", "title": "",
             "qty": 1700, "cost": 977.5, "entry_price": .575, "fair_entry": .593, "halved": 0, "realized": 0.0}
    bot.db.save_dir_position(young)  # opened now
    bot._quotes["e4"] = (.42, .43)
    d.step()
    assert "S-ME" in d.positions()


def test_second_bot_on_the_same_database_refuses_to_start(tmp_path):
    from sigbot.arb_bot import ArbBot
    a, b = ArbBot.__new__(ArbBot), ArbBot.__new__(ArbBot)
    a.s = b.s = SimpleNamespace(db_path=tmp_path / "sig.db")
    a._acquire_lock()
    with pytest.raises(SystemExit, match="Another bot is already running"):
        b._acquire_lock()


# ---- topping up a race already held ----

def legacy(race="S-RI", view="D", ex="e2", qty=526, entry=.923):
    return {"mode": "paper", "race": race, "view": view, "exchange_id": ex, "market_id": None, "title": "",
            "qty": qty, "cost": qty * entry, "entry_price": entry, "fair_entry": .988, "halved": 0, "realized": 0.0}


def test_tops_up_a_small_position_to_the_race_cap_with_averaged_entry(tmp_path):
    d, bot = make(tmp_path, dcfg=DirectionalConfig(directional=True, max_race=2_000))
    bot.db.save_dir_position(legacy())
    d.step()
    p = d.positions()["S-RI"]
    assert p["qty"] > 526 and p["cost"] <= 2_000 + 1  # topped up, within the race cap
    assert p["entry_price"] == pytest.approx(p["cost"] / p["qty"])  # averaged
    assert .923 < p["entry_price"] < .93  # between the old .923 and the new .93


def test_never_bets_the_opposite_way_in_a_held_race(tmp_path):
    d, bot = make(tmp_path)
    bot.db.save_dir_position(legacy(view="R", ex="e1", entry=.06))  # held as "R wins"
    d._manage = lambda p: None  # entries only: its own exit rules would close it first
    d.step()
    p = d.positions()["S-RI"]
    assert p["view"] == "R" and p["qty"] == 526  # the cheap "D wins" side wasn't added


def test_recently_topped_up_position_is_protected_from_recycling(tmp_path):
    from datetime import datetime, timezone
    d, bot = make(tmp_path, dcfg=DirectionalConfig(directional=True, budget=1_000, max_race=1_000))
    old = {"mode": "paper", "race": "S-ME", "view": "D", "exchange_id": "e4", "market_id": "4", "title": "",
           "qty": 1700, "cost": 977.5, "entry_price": .575, "fair_entry": .593, "halved": 0, "realized": 0.0,
           "opened": "2026-10-01T00:00:00+00:00",  # opened long ago...
           "last_buy": datetime.now(timezone.utc).isoformat(timespec="seconds")}  # ...but added to just now
    bot.db.save_dir_position(old)
    bot._quotes["e4"] = (.42, .43)
    d.step()
    assert "S-ME" in d.positions()


def test_nearly_out_of_cash_only_buys_the_best_gap(tmp_path, monkeypatch):
    fake = FakeOrders()
    monkeypatch.setattr(directional, "orders", fake)
    dcfg = DirectionalConfig(directional=True, live_budget=50_000, live_max_race=10_000, cash_reserve=2_000)
    d, bot = make(tmp_path, live=True, dcfg=dcfg)
    bot.cash = 2_600  # 600 above the reserve: below the 1,000 "nearly out" line
    d.step()
    assert {c[0] for c in fake.calls} == {"e2"}  # RI only (best return on capital), no ME top-up


def test_a_partly_filled_half_sale_is_tried_again(tmp_path, monkeypatch):
    class Thin(FakeOrders):  # only 51 shares bid at the price
        def place_limit(self, client, ex, side, action, qty, price, tid, ttl_seconds=None):
            got = min(qty, 51) if action == "sell" else qty
            self.calls.append((ex, side, action, qty, round(price, 3)))
            return {"orderId": len(self.calls), "open": False, "quantityTraded": got, "totalCost": got * price}
    fake = Thin()
    monkeypatch.setattr(directional, "orders", fake)
    d, bot = make(tmp_path, live=True)
    d.step()
    p = d.positions()["S-RI"]
    fair = next(e.fair for e in bot._edges if e.race == "S-RI" and e.view == "D")
    bot._quotes["e2"] = (0.0, round(1 - (p["entry_price"] + 0.6 * (fair - p["entry_price"])), 3))
    d._manage(p)
    after = d.positions()["S-RI"]
    assert after["qty"] == p["qty"] - 51 and after["halved"] == 0  # not done: try again
    d._manage(after)
    assert d.positions()["S-RI"]["qty"] == p["qty"] - 102
