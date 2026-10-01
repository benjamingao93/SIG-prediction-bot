import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigbot import arb_bot
from sigbot.api.models import Exchange, Level, Market, OrderBook
from sigbot.config import ArbConfig
from sigbot.data.external.races import Race
from sigbot.trading import arb


def market(mid, party, race="Texas Senate"):
    return Market(mid, f"Will the {party} Party win the {race}?", "open", None, None, [], False,
                  [Exchange(f"e{mid}", "YES", None)])


def book(ex, bids=(), asks=()):
    return OrderBook(ex, "", [Level(p, q) for p, q in bids], [Level(p, q) for p, q in asks])


def test_baskets_group_races_and_mark_exhaustive_only_when_confirmed():
    ms = [market("1", "Democratic"), market("2", "Republican"),
          market("3", "Democratic", "Nebraska Senate"), market("4", "Republican", "Nebraska Senate"),
          market("5", "Independent", "Nebraska Senate"), market("6", "Democratic", "Ohio Senate")]
    races = {"S-TX": Race("S-TX", "senate", -6), "S-NE": Race("S-NE", "senate", -10, has_d=False)}
    bs = {b.key: b for b in arb.build_baskets(ms, races)}
    assert set(bs) == {"S-TX", "S-NE"}  # Ohio has a single market: nothing to arb
    assert bs["S-TX"].exhaustive and not bs["S-NE"].exhaustive
    assert bs["S-NE"].payout("no") == 2 and bs["S-NE"].payout("yes") == 1
    assert not arb.build_baskets(ms)[0].exhaustive  # no races.csv: never assume exhaustive


def test_screen_needs_bids_above_one_and_yes_only_when_allowed():
    b = arb.build_baskets([market("1", "Democratic"), market("2", "Republican")],
                          {"S-TX": Race("S-TX", "senate", -6)})[0]
    assert arb.screen(b, {"e1": (.62, .63), "e2": (.385, .39)}, .005, False) is None  # 1.005: not > 1.005
    assert arb.screen(b, {"e1": (.625, .63), "e2": (.385, .39)}, .005, False) == "no"
    cheap = {"e1": (.5, .55), "e2": (.3, .4)}  # asks sum to .95
    assert arb.screen(b, cheap, .005, False) is None
    assert arb.screen(b, cheap, .005, True) == "yes"


def test_size_walks_levels_until_profit_runs_out():
    b = arb.build_baskets([market("1", "Democratic"), market("2", "Republican")])[0]
    books = {"e1": book("e1", bids=[(.63, 100), (.62, 500)]),
             "e2": book("e2", bids=[(.39, 300), (.37, 1000)])}
    # NO costs: e1 .37 (100) then .38 (500); e2 .61 (300) then .63
    o = arb.size(b, "no", books, min_profit=.005, max_sets=10_000)
    # set 1-100: .37+.61=.98 → +.02; 101-300: .38+.61=.99 → +.01; then .38+.63 = 1.01 → stop
    assert o.sets == 300
    assert o.limits == (.38, .61)
    assert o.cost == pytest.approx(100 * .98 + 200 * .99)
    assert o.profit == pytest.approx(100 * .02 + 200 * .01)
    assert arb.size(b, "no", books, .005, 50).sets == 50


def test_three_way_no_basket_pays_two_per_set():
    ms = [market("1", "Democratic", "Nebraska Senate"), market("2", "Republican", "Nebraska Senate"),
          market("3", "Independent", "Nebraska Senate")]
    b = arb.build_baskets(ms)[0]
    books = {"e1": book("e1", bids=[(.025, 1301)]), "e2": book("e2", bids=[(.72, 702)]),
             "e3": book("e3", bids=[(.265, 5183)])}
    o = arb.size(b, "no", books, .005, 10_000)
    assert o.sets == 702 and o.payout == 1404
    assert o.profit == pytest.approx(702 * .01)


# ---- live execution against a fake exchange ----

class FakeOrders:
    def __init__(self, fills):
        self.fills = list(fills)  # one list of per-leg traded quantities per multi-leg call
        self.calls = []
        self.cancelled = []

    def place_multi_leg(self, client, legs, tid, ttl_seconds=None):
        self.calls.append(legs)
        got = self.fills.pop(0)
        return [{"orderId": i, "open": g < l["quantity"], "quantityTraded": g,
                 "totalCost": g * l["price"]} for i, (g, l) in enumerate(zip(got, legs))]

    def cancel(self, client, oid):
        self.cancelled.append(oid)


def live_bot(monkeypatch, tmp_path, fills, books=None, fills_api=None):
    from sigbot.data.db import DB
    fake = FakeOrders(fills)
    monkeypatch.setattr(arb_bot, "orders", fake)
    monkeypatch.setattr(arb_bot.mk, "get_orderbook", lambda client, ex, tid: books[ex])
    bot = arb_bot.ArbBot.__new__(arb_bot.ArbBot)
    bot.s = SimpleNamespace(kill_switch=Path(tmp_path / "KILL"))
    bot.client = SimpleNamespace(get=lambda path: {"data": fills_api or []},
                                 reads=SimpleNamespace(available=100))
    bot.db = DB(tmp_path / "t.db")
    bot.live, bot.cfg, bot.tid = True, ArbConfig(), "t"
    bot.cash, bot.spent, bot.repairs, bot.stale_skips = 1e6, 0.0, {}, 0
    b = arb.build_baskets([market("1", "Democratic"), market("2", "Republican")])[0]
    # planned: 100 sets at .38 + .61 = .99 → +.01 per set
    return bot, fake, arb.ArbOrder(b, "no", 100, (.38, .61), 99.0)


def test_live_clean_fill(monkeypatch, tmp_path):
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 100]])
    assert bot.execute(order)
    assert len(fake.calls) == 1 and not fake.cancelled
    assert bot.db.get_repairs() == {}
    assert bot.spent == pytest.approx(99.0)


def test_lopsided_fill_is_repaired_from_the_fresh_book(monkeypatch, tmp_path):
    # e2 NO now costs .62 (YES bid .38): within the cap .61 + .01 profit + .02 slippage = .64
    books = {"e2": book("e2", bids=[(.38, 1000)])}
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40], [60]], books)
    bot.execute(order)
    assert fake.cancelled == [1]  # resting remainder of leg 2 pulled
    assert fake.calls[1] == [{"exchangeId": "e2", "side": "no", "quantity": 60, "price": pytest.approx(.62)}]
    assert bot.db.get_repairs() == {}  # hedged


def test_repair_waits_for_a_price_under_the_cap_and_survives_restarts(monkeypatch, tmp_path):
    books = {"e2": book("e2", bids=[(.30, 1000)])}  # NO at .70: over the .64 cap
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40], [60]], books)
    bot.execute(order)
    assert len(fake.calls) == 1  # no repair order at a loss beyond the cap
    saved = bot.db.get_repairs()["S-TX"]
    assert saved["legs"][0]["short"] == 60 and saved["legs"][0]["cap"] == pytest.approx(.64)
    books["e2"] = book("e2", bids=[(.37, 25), (.36, 1000)])  # NO .63 then .64
    bot.repairs = bot.db.get_repairs()  # as a new process would load it
    bot.work_repair("S-TX")
    assert fake.calls[1][0]["quantity"] == 60 and fake.calls[1][0]["price"] == pytest.approx(.64)
    assert bot.db.get_repairs() == {}


def test_partial_repair_keeps_the_rest_pending(monkeypatch, tmp_path):
    books = {"e2": book("e2", bids=[(.38, 1000)])}
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40], [25]], books)
    bot.execute(order)
    assert bot.db.get_repairs()["S-TX"]["legs"][0]["short"] == 35


def test_repairs_go_ahead_on_slow_books_because_the_cap_bounds_the_price(monkeypatch, tmp_path):
    books = {"e2": book("e2", bids=[(.38, 1000)])}
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40], [60]], books)
    bot.cfg = ArbConfig(max_book_age=-1)  # everything counts as stale
    bot.execute(order)
    assert len(fake.calls) == 2 and fake.calls[1][0]["price"] <= .64
    assert bot.db.get_repairs() == {}


def test_late_no_fills_count_despite_negative_quantities(monkeypatch, tmp_path):
    bot, fake, order = live_bot(monkeypatch, tmp_path, [], fills_api=[{"quantity": -30}, {"quantity": -10}])
    assert bot._settle_leg({"orderId": 7, "open": True, "quantityTraded": 0}) == 40


def test_repair_waits_when_the_read_budget_is_low(monkeypatch, tmp_path):
    books = {"e2": book("e2", bids=[(.38, 1000)])}
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40]], books)
    bot.client.reads.available = 3  # only the bulk-price reserve left
    bot.execute(order)
    assert len(fake.calls) == 1 and bot.stale_skips == 0  # no read, no stale skip: just next cycle
    assert bot.db.get_repairs()["S-TX"]["legs"][0]["short"] == 60


def test_step_reads_best_edge_first_and_stops_at_the_budget(monkeypatch, tmp_path):
    ms = [market("1", "Democratic"), market("2", "Republican"),
          market("3", "Democratic", "Ohio Senate"), market("4", "Republican", "Ohio Senate")]
    quotes = [{"exchangeId": "e1", "bestBid": .62, "bestAsk": .63}, {"exchangeId": "e2", "bestBid": .40, "bestAsk": .41},
              {"exchangeId": "e3", "bestBid": .55, "bestAsk": .56}, {"exchangeId": "e4", "bestBid": .50, "bestAsk": .51}]
    read = []
    books = {e: book(e) for e in ("e1", "e2", "e3", "e4")}  # empty: nothing to size
    bot, fake, _ = live_bot(monkeypatch, tmp_path, [], books)
    monkeypatch.setattr(arb_bot.mk, "bulk_prices", lambda c, ids, tid: quotes)
    def get_orderbook(c, ex, tid):
        read.append(ex)
        bot.client.reads.available -= 1
        return books[ex]
    monkeypatch.setattr(arb_bot.mk, "get_orderbook", get_orderbook)
    bot.baskets = arb.build_baskets(ms)
    bot._last_refresh, bot._last_trade, bot._quotes = time.monotonic(), {}, {}
    bot.client.reads.available = 5  # room for one basket (2 reads) + the 3 reserved
    bot.step()
    assert read == ["e3", "e4"]  # Ohio (Σbid 1.05) before Texas (1.02); then the budget is spent
    assert set(bot._last_trade) == {"S-OH"}  # Texas wasn't read, so it isn't cooling down
