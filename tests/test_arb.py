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
    bot.held, bot.held_cost, bot.exits, bot._positions_at = {}, {}, 0, time.monotonic()
    bot.quoter = None
    bot.feed, bot.feed_hits, bot.feed_misses = None, 0, 0
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


# ---- exits ----

def two_leg_basket():
    return arb.build_baskets([market("1", "Democratic"), market("2", "Republican")])[0]


def test_held_baskets_need_even_no_on_every_leg():
    b = two_leg_basket()
    ohio = arb.build_baskets([market("3", "Democratic", "Ohio Senate"), market("4", "Republican", "Ohio Senate")])[0]
    held = arb.held_baskets([b, ohio], {"e1": 100, "e2": 100, "e3": 50, "e4": 20})
    assert held == {"S-TX": 100}  # Ohio is uneven: not a basket
    assert arb.held_baskets([b], {"e1": 100}) == {}  # one leg only


def test_exit_bar_is_the_lower_of_beat_holding_and_beat_cost():
    b = two_leg_basket()
    assert arb.exit_bar(b, None, .005, .005) == pytest.approx(1.005)  # cost unknown: beat holding only
    assert arb.exit_bar(b, .99, .005, None) == pytest.approx(1.005)  # early exits off
    assert arb.exit_bar(b, .99, .005, .005) == pytest.approx(.995)  # early: cost .99 + .005
    assert arb.exit_bar(b, 1.006, .005, .005) == pytest.approx(1.005)  # a losing set: only beat holding


def test_screen_exit_against_the_bar():
    b = two_leg_basket()
    # proceeds = Σ(1 − ask): asks .61 + .395 → .995
    q = {"e1": (.6, .61), "e2": (.38, .395)}
    assert arb.screen_exit(b, q, bar=1.005) is None  # doesn't beat holding
    assert arb.screen_exit(b, q, bar=.995) == pytest.approx(0)  # exactly the early-exit bar: sell
    assert arb.screen_exit(b, q, bar=.996) is None
    assert arb.screen_exit(b, {"e1": (.6, None), "e2": (.38, .395)}, bar=.9) is None


def test_size_exit_walks_asks_until_the_bar_and_caps_at_held_sets():
    b = two_leg_basket()
    # selling NO = 1 − YES ask: e1 .40 (100) then .395; e2 .61 (300) then .60
    books = {"e1": book("e1", asks=[(.60, 100), (.605, 500)]), "e2": book("e2", asks=[(.39, 300), (.40, 50)])}
    o = arb.size_exit(b, books, held_sets=10_000, bar=1.005, max_sets=10_000)
    # beat holding: 1-100 at 1.01 ✓; 101-300 at 1.005 ✓ (meets the bar); then .395+.60 = .995 ✗
    assert o.sets == 300 and o.proceeds == pytest.approx(100 * 1.01 + 200 * 1.005)
    early = arb.size_exit(b, books, held_sets=10_000, bar=.995, max_sets=10_000, cost_per_set=.99)
    assert early.sets == 350 and early.realized == pytest.approx(early.proceeds - 350 * .99)
    assert arb.size_exit(b, books, held_sets=40, bar=1.005, max_sets=10_000).sets == 40


def test_step_takes_an_early_exit_when_a_sale_beats_cost(monkeypatch, tmp_path):
    quotes = [{"exchangeId": "e1", "bestBid": .59, "bestAsk": .61}, {"exchangeId": "e2", "bestBid": .38, "bestAsk": .395}]
    books = {"e1": book("e1", asks=[(.61, 1000)]), "e2": book("e2", asks=[(.395, 1000)])}
    bot, fake, _ = live_bot(monkeypatch, tmp_path, [[50, 50]], books)
    monkeypatch.setattr(arb_bot.mk, "bulk_prices", lambda c, ids, tid: quotes)
    bot.baskets = [two_leg_basket()]
    bot.held, bot.held_cost = {"S-TX": 50}, {"S-TX": .99}  # proceeds .995 = cost + .005
    bot._last_refresh, bot._last_trade, bot._quotes = time.monotonic(), {}, {}
    bot.cfg = ArbConfig(exit_early=False)
    bot.step()
    assert fake.calls == []  # doesn't beat holding (needs 1.005)
    bot.cfg, bot._last_trade = ArbConfig(), {}
    bot.step()
    assert fake.calls[0][0]["action"] == "sell" and fake.calls[0][0]["quantity"] == 50


def test_live_exit_sends_sell_legs(monkeypatch, tmp_path):
    bot, fake, _ = live_bot(monkeypatch, tmp_path, [[100, 100]])
    bot.held, bot.held_cost, bot.spent = {"S-TX": 100}, {"S-TX": .99}, 99.0
    order = arb.ExitOrder(two_leg_basket(), 100, (.40, .61), 101.0)
    assert bot.execute_exit(order)
    assert [l["action"] for l in fake.calls[0]] == ["sell", "sell"]
    assert [l["side"] for l in fake.calls[0]] == ["no", "no"]
    assert bot.spent == pytest.approx(0) and bot.exits == 1
    assert bot.db.get_repairs() == {}


def test_uneven_exit_buys_back_the_oversold_leg(monkeypatch, tmp_path):
    # e1 sold 100, e2 only 30: e1 now holds 70 less NO than e2 → buy 70 NO back on e1
    books = {"e1": book("e1", bids=[(.59, 1000)])}  # NO at .41 ≤ cap .40 + .02
    bot, fake, _ = live_bot(monkeypatch, tmp_path, [[100, 30], [70]], books)
    bot.held_cost = {"S-TX": .99}
    bot.execute_exit(arb.ExitOrder(two_leg_basket(), 100, (.40, .61), 101.0))
    assert fake.calls[1] == [{"exchangeId": "e1", "side": "no", "quantity": 70, "price": pytest.approx(.41)}]
    assert bot.db.get_repairs() == {}


def test_paper_exit_sends_nothing(monkeypatch, tmp_path):
    bot, fake, _ = live_bot(monkeypatch, tmp_path, [])
    bot.live = False
    assert bot.execute_exit(arb.ExitOrder(two_leg_basket(), 100, (.40, .61), 101.0))
    assert fake.calls == [] and bot.exits == 1


def test_step_exits_held_basket_and_kill_switch_blocks_it(monkeypatch, tmp_path):
    quotes = [{"exchangeId": "e1", "bestBid": .59, "bestAsk": .60}, {"exchangeId": "e2", "bestBid": .38, "bestAsk": .39}]
    books = {"e1": book("e1", asks=[(.60, 1000)]), "e2": book("e2", asks=[(.39, 1000)])}
    bot, fake, _ = live_bot(monkeypatch, tmp_path, [[50, 50]], books)
    monkeypatch.setattr(arb_bot.mk, "bulk_prices", lambda c, ids, tid: quotes)
    bot.baskets = [two_leg_basket()]
    bot.held, bot.held_cost = {"S-TX": 50}, {"S-TX": .99}
    bot._last_refresh, bot._last_trade, bot._quotes = time.monotonic(), {}, {}
    (tmp_path / "KILL").touch()
    bot.step()
    assert fake.calls == []
    (tmp_path / "KILL").unlink()
    bot.step()
    assert fake.calls[0][0]["action"] == "sell" and fake.calls[0][0]["quantity"] == 50


# ---- realtime feed in the bot ----

def test_books_come_from_the_feed_when_trusted_else_rest(monkeypatch, tmp_path):
    from sigbot.api.realtime import FeedRunner
    rest_reads = []
    bot, _, _ = live_bot(monkeypatch, tmp_path, [], books={"e1": book("e1", bids=[(.5, 10)]), "e2": book("e2")})
    monkeypatch.setattr(arb_bot.mk, "get_orderbook", lambda c, ex, tid: rest_reads.append(ex) or book(ex))
    bot.feed = FeedRunner(SimpleNamespace(reads=SimpleNamespace(available=100)), "t")
    for ex, mid in (("e1", "1"), ("e2", "2")):
        bot.feed.store.apply(mid, {"exchangeId": ex, "asOf": {"sequence": 1, "at": None},
                                   "bids": [{"price": .61, "quantity": 5}], "asks": []})
    bot.feed.healthy, bot.feed.subscribed = True, {"1", "2"}
    bot.feed.synced_at = {"1": time.monotonic(), "2": time.monotonic()}
    books, read_at = bot._books(["e1", "e2"])
    assert rest_reads == [] and books["e1"].best_bid == .61 and bot.feed_hits == 1
    assert not bot._stale(read_at, "x")  # current as of now: never skipped as stale
    bot.feed.subscribed = {"1"}  # e2's market dropped: fall back to REST for the race
    bot._books(["e1", "e2"])
    assert sorted(rest_reads) == ["e1", "e2"] and bot.feed_misses == 1


def test_watchlist_puts_held_and_repairing_races_first_and_caps_markets(monkeypatch, tmp_path):
    from sigbot.api.realtime import FeedRunner
    bot, _, _ = live_bot(monkeypatch, tmp_path, [])
    races = ["Texas Senate", "Ohio Senate", "Iowa Senate", "Maine Senate"]
    ms = [market(f"{i}{p[0]}", p, r) for i, r in enumerate(races) for p in ("Democratic", "Republican")]
    bot.baskets = arb.build_baskets(ms)
    bot.feed = FeedRunner(SimpleNamespace(reads=SimpleNamespace(available=100)), "t")
    bot.cfg = ArbConfig(feed_markets=4)
    bot.held, bot.repairs = {"S-ME": 10}, {"S-IA": {}}
    quotes = {f"e{i}{p}": (.49, .5) for i in range(4) for p in "DR"}
    quotes.update({"e1D": (.52, .53)})  # Ohio closest to a buy trigger
    bot._update_watch(quotes)
    assert set(bot.feed.market_ids) == {"2D", "2R", "3D", "3R"}  # held + repairing fill the 4 slots
    bot.cfg = ArbConfig(feed_markets=6)
    bot._update_watch(quotes)
    assert {"1D", "1R"} <= set(bot.feed.market_ids)  # then the race nearest a trigger


def test_bot_wakes_for_pushed_fills_before_the_next_cycle(monkeypatch, tmp_path):
    import threading
    bot, _, _ = live_bot(monkeypatch, tmp_path, [])
    handled = []
    bot._wake, bot._quotes = threading.Event(), {}
    bot.quoter = SimpleNamespace(process_pushed=lambda quotes: handled.append(time.monotonic()))
    t0 = time.monotonic()
    threading.Timer(0.05, bot._wake.set).start()
    bot._wait_until(t0 + 0.5)
    assert handled and handled[0] - t0 < 0.3  # handled at the push, then waited out the cycle
    assert time.monotonic() - t0 >= 0.5


def test_depth_fraction_leaves_a_cushion_on_every_level():
    b = arb.build_baskets([market("1", "Democratic"), market("2", "Republican")])[0]
    books = {"e1": book("e1", bids=[(.63, 100), (.62, 500)]), "e2": book("e2", bids=[(.39, 300), (.37, 1000)])}
    full = arb.size(b, "no", books, .005, 10_000)
    half = arb.size(b, "no", books, .005, 10_000, depth_fraction=.5)
    assert full.sets == 300 and half.sets == 150  # 50 @ +.02 then 100 @ +.01, never the whole level
    assert half.limits == full.limits
