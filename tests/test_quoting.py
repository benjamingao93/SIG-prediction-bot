import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigbot import quoter as quoter_mod
from sigbot.api.models import Exchange, Level, Market, OrderBook
from sigbot.config import ArbConfig
from sigbot.data.db import DB
from sigbot.quoter import Quoter
from sigbot.trading import arb, quoting


def market(mid, party, race="Texas Senate"):
    return Market(mid, f"Will the {party} Party win the {race}?", "open", None, None, [], False,
                  [Exchange(f"e{mid}", "YES", None)])


def book(ex, bids=(), asks=()):
    return OrderBook(ex, "", [Level(p, q) for p, q in bids], [Level(p, q) for p, q in asks])


TX = arb.build_baskets([market("1", "Democratic"), market("2", "Republican")])[0]  # legs: D=e1, R=e2
# D: YES .59/.62 → NO .38/.41.  R: YES .39/.41 → NO .59/.61
Q = {"e1": (.59, .62), "e2": (.39, .41)}


# ---- pricing ----

def test_two_leg_quote_prices_off_the_other_legs_bid():
    s = quoting.quote_price(TX, 0, Q, edge=.005)  # bid NO on D, hedge with R's NO at 1 − .39
    assert s.price == pytest.approx(.385) and s.hedge_asks == (("e2", .61),)
    assert s.edge() == pytest.approx(.005) and s.score == pytest.approx(.005)  # .385 vs NO bid .38
    assert quoting.quote_price(TX, 1, Q, edge=.005) is None  # R: .585 is below R's NO bid .59
    assert quoting.best_leg(TX, Q, .005).leg == 0


def test_price_rounds_down_to_the_tick():
    s = quoting.quote_price(TX, 0, {"e1": (.59, .63), "e2": (.3926, .41)}, edge=.005)
    assert s.price == pytest.approx(.385)  # raw .3876 → .385, so the edge only grows
    assert s.edge() > .005


def test_no_quote_where_it_would_be_a_taker_arb_or_behind_the_queue():
    taker = {"e1": (.62, .63), "e2": (.39, .41)}  # bids sum 1.01: buy both NO now, don't rest
    assert quoting.quote_price(TX, 0, taker, .005) is None
    behind = {"e1": (.59, .615), "e2": (.39, .41)}  # D NO bid .385: our .385 only joins it
    assert quoting.quote_price(TX, 0, behind, .005) is None


def test_three_leg_quote_pays_two_per_set():
    ne = arb.build_baskets([market("3", "Democratic", "Nebraska Senate"), market("4", "Republican", "Nebraska Senate"),
                            market("5", "Independent", "Nebraska Senate")])[0]
    q = {"e3": (.02, .03), "e4": (.71, .73), "e5": (.26, .28)}
    s = quoting.best_leg(ne, q, .005)  # legs sort D, I, R
    assert ne.legs[s.leg].label == "I" and s.price == pytest.approx(.725)  # 2 − .98 − .29 − .005
    assert s.edge() == pytest.approx(.005)


def test_reprice_sizes_by_hedge_depth_and_drops_dead_quotes():
    spec = quoting.quote_price(TX, 0, Q, .005)
    s, size = quoting.reprice(spec, {"e2": book("e2", bids=[(.39, 120)])}, Q, .005, max_size=200)
    assert s.price == pytest.approx(.385) and size == 120
    assert quoting.reprice(spec, {"e2": book("e2", bids=[(.38, 500)])}, Q, .005, 200) is None  # hedge moved


def test_hedge_caps_are_break_even_plus_slippage():
    assert quoting.hedge_caps(quoting.quote_price(TX, 0, Q, .005), .005, .02) == {"e2": pytest.approx(.635)}


def test_select_quotes_ranks_and_excludes():
    oh = arb.build_baskets([market("6", "Democratic", "Ohio Senate"), market("7", "Republican", "Ohio Senate")])[0]
    q = {**Q, "e6": (.50, .56), "e7": (.47, .48)}  # Ohio: quote D NO at .465 vs NO bid .44 → score .025
    picks = quoting.select_quotes([TX, oh], q, 1, .005)
    assert [p.basket.key for p in picks] == ["S-OH"]
    assert [p.basket.key for p in quoting.select_quotes([TX, oh], q, 2, .005, exclude=["S-OH"])] == ["S-TX"]


def test_paper_fill_rule():
    assert quoting.would_fill(.385, .60, .615)  # a trade printed at our YES offer 1 − .385
    assert not quoting.would_fill(.385, .615, .615)  # no new trade
    assert not quoting.would_fill(.385, .60, .61)  # traded below our offer


# ---- quote manager ----

class FakeOrders:
    def __init__(self):
        self.placed, self.cancelled = [], []

    def place_limit(self, client, ex, side, action, qty, price, tid, ttl_seconds=None):
        self.placed.append((ex, side, action, qty, price))
        return {"orderId": 100 + len(self.placed)}

    def cancel(self, client, oid):
        self.cancelled.append(oid)


def make(monkeypatch, tmp_path, live, hedge_depth=500, **cfg):
    fake = FakeOrders()
    monkeypatch.setattr(quoter_mod, "orders", fake)
    fills = {"data": []}
    books = {"e1": book("e1", bids=[(.59, 500)]), "e2": book("e2", bids=[(.39, hedge_depth)])}
    bot = SimpleNamespace(
        live=live, cfg=ArbConfig(quoting=True, **cfg), baskets=[TX], db=DB(tmp_path / "t.db"), repairs={},
        cash=1e6, tid="t", s=SimpleNamespace(kill_switch=Path(tmp_path / "KILL"), tournament_slug="x"),
        client=SimpleNamespace(get=lambda path, **kw: fills), worked=[], feed=None)
    bot._can_read = lambda n: True
    bot._books = lambda exs: ({ex: books[ex] for ex in exs}, time.monotonic())
    bot.work_repair = lambda race: bot.worked.append(race)
    return Quoter(bot), bot, fake, fills, books


def test_paper_quote_fills_and_records_simulated_profit(monkeypatch, tmp_path):
    q, bot, fake, _, _ = make(monkeypatch, tmp_path, live=False)
    q.step(Q, {"e1": .60})
    assert set(q.active) == {"S-TX"} and q.active["S-TX"]["price"] == pytest.approx(.385)
    assert fake.placed == []  # paper: nothing sent
    q.step(Q, {"e1": .615})  # someone lifted YES at our offer
    assert q.stats["fills"] == 1 and q.stats["filled_sets"] == 200
    assert q.stats["paper_pnl"] == pytest.approx(200 * .005)
    assert q.stats["placed"] == 2  # no hedge to wait for in paper: the race is quoted again


def test_live_fill_is_hedged_through_the_repair_system(monkeypatch, tmp_path):
    q, bot, fake, fills, _ = make(monkeypatch, tmp_path, live=True)
    q.step(Q, {})
    assert fake.placed == [("e1", "no", "buy", 200, pytest.approx(.385))]
    fills["data"] = [{"orderId": 101, "quantity": -50}]  # NO fills come back negative
    q.step(Q, {})
    rep = bot.db.get_repairs()["S-TX"]
    assert rep["legs"] == [{"exchange_id": "e2", "title": TX.legs[1].title, "short": 50, "cap": pytest.approx(.635)}]
    assert bot.worked == ["S-TX"]
    assert "S-TX" not in q.active  # its race has a hedge pending: quote pulled until hedged
    assert fake.cancelled == [101]
    fills["data"].append({"orderId": 101, "quantity": -30})  # landed before the cancel
    bot.repairs = {"S-TX": rep}
    q.step(Q, {})
    assert bot.db.get_repairs()["S-TX"]["legs"][0]["short"] == 80  # added, not overwritten


def test_quote_pulled_at_once_when_the_hedge_gets_dearer(monkeypatch, tmp_path):
    q, bot, fake, _, _ = make(monkeypatch, tmp_path, live=True, quote_writes_per_cycle=1)
    q.step(Q, {})
    moved = {"e1": (.59, .62), "e2": (.385, .41)}  # R NO ask now .615: a fill at .385 earns 0
    q.step(moved, {})
    assert fake.cancelled == [101] and "S-TX" not in q.active


def test_upward_reprice_waits_for_min_life(monkeypatch, tmp_path):
    q, bot, fake, _, books = make(monkeypatch, tmp_path, live=True, quote_min_life=20)
    q.step(Q, {})
    better = {"e1": (.59, .63), "e2": (.40, .41)}  # could now bid .395
    q.step(better, {})
    assert fake.cancelled == []  # too young
    q.active["S-TX"]["placed"] -= 30
    books["e2"] = book("e2", bids=[(.40, 500)])
    q.step(better, {})
    assert fake.cancelled == [101] and q.active["S-TX"]["price"] == pytest.approx(.395)


def test_kill_switch_cancels_everything_and_places_nothing(monkeypatch, tmp_path):
    q, bot, fake, _, _ = make(monkeypatch, tmp_path, live=True)
    q.step(Q, {})
    (tmp_path / "KILL").touch()
    q.step(Q, {})
    assert fake.cancelled == [101] and q.active == {} and len(fake.placed) == 1


def test_size_capped_by_hedge_depth(monkeypatch, tmp_path):
    q, bot, fake, _, _ = make(monkeypatch, tmp_path, live=True, hedge_depth=75)
    q.step(Q, {})
    assert fake.placed[0][3] == 75


def test_startup_cancels_leftovers_and_hedges_their_fills(monkeypatch, tmp_path):
    q, bot, fake, fills, _ = make(monkeypatch, tmp_path, live=True)
    q.step(Q, {})
    left = bot.db.get_quotes()["S-TX"]
    q2, *_ = make(monkeypatch, tmp_path, live=True)
    q2.bot.db = bot.db
    fills["data"] = [{"orderId": left["order_id"], "quantity": -40}]
    q2.bot.client = SimpleNamespace(get=lambda path, **kw: fills)
    q2.startup()
    assert quoter_mod.orders.cancelled == [left["order_id"]]
    assert bot.db.get_quotes() == {} and bot.db.get_repairs()["S-TX"]["legs"][0]["short"] == 40


def test_add_to_repair_merges(tmp_path):
    db = DB(tmp_path / "t.db")
    db.add_to_repair("S-TX", "no", [{"exchange_id": "e2", "title": "R", "short": 100, "cap": .60}], "quote")
    rep = db.add_to_repair("S-TX", "no", [{"exchange_id": "e2", "title": "R", "short": 100, "cap": .64}], "quote")
    assert rep["legs"][0]["short"] == 200 and rep["legs"][0]["cap"] == pytest.approx(.62)


# ---- account-channel fills and confirmed cancels ----

from sigbot.api.client import SigAPIError


class FakeFeed:
    def __init__(self, trusted=True):
        self.trusted, self.pushed, self.user_need_poll = trusted, [], False

    def user_trusted(self):
        return self.trusted

    def pop_fills(self):
        out, self.pushed = self.pushed, []
        return out


def counting(bot, fills):
    calls = []
    bot.client = SimpleNamespace(get=lambda path, **kw: calls.append(path) or fills)
    return calls


def test_trusted_channel_skips_routine_fill_reads_until_one_of_ours_is_pushed(monkeypatch, tmp_path):
    q, bot, fake, fills, _ = make(monkeypatch, tmp_path, live=True)
    bot.feed = FakeFeed(trusted=True)
    q.step(Q, {})  # places the quote
    calls = counting(bot, fills)
    q._last_poll = time.monotonic()
    q.step(Q, {})
    assert calls == []  # nothing pushed, safety read not due yet
    bot.feed.pushed = [{"orderId": 999, "quantity": -5}]  # someone else's fill
    q.step(Q, {})
    assert calls == []
    fills["data"] = [{"orderId": 101, "quantity": -60}]
    bot.feed.pushed = [{"orderId": 101, "quantity": -60}]
    q.process_pushed(Q)  # what the bot's wake-up calls
    assert len(calls) == 1 and bot.db.get_repairs()["S-TX"]["legs"][0]["short"] == 60


def test_untrusted_channel_reads_fills_every_cycle(monkeypatch, tmp_path):
    q, bot, fake, fills, _ = make(monkeypatch, tmp_path, live=True)
    bot.feed = FakeFeed(trusted=False)
    q.step(Q, {})
    calls = counting(bot, fills)
    q._last_poll = time.monotonic()
    q.step(Q, {})
    q.step(Q, {})
    assert len(calls) == 2


def test_unconfirmed_cancel_is_retried_and_its_fills_still_hedged(monkeypatch, tmp_path):
    q, bot, fake, fills, _ = make(monkeypatch, tmp_path, live=True)
    q.step(Q, {})
    attempts = []

    def flaky_cancel(client, oid):
        attempts.append(oid)
        if len(attempts) == 1:
            raise SigAPIError(503, "SERVICE_UNAVAILABLE", "busy")
    monkeypatch.setattr(quoter_mod.orders, "cancel", flaky_cancel)
    q.cancel_all("test")
    assert "S-TX@101" in bot.db.get_quotes()  # kept for a restart until confirmed
    fills["data"] = [{"orderId": 101, "quantity": -20}]  # it filled while still resting
    q._detect_live(Q)
    assert attempts == [101, 101] and q.closing == []
    assert bot.db.get_repairs()["S-TX"]["legs"][0]["short"] == 20
    assert "S-TX@101" not in bot.db.get_quotes()
