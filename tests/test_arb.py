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


def live_bot(monkeypatch, tmp_path, fills):
    fake = FakeOrders(fills)
    monkeypatch.setattr(arb_bot, "orders", fake)
    bot = arb_bot.ArbBot.__new__(arb_bot.ArbBot)
    bot.s = SimpleNamespace(kill_switch=Path(tmp_path / "KILL"))
    bot.client = SimpleNamespace(get=lambda path: {"data": []})
    bot.db = SimpleNamespace(log_signal=lambda **kw: None)
    bot.live, bot.cfg, bot.tid = True, ArbConfig(), "t"
    bot.cash, bot.spent, bot.frozen = 1e6, 0.0, set()
    b = arb.build_baskets([market("1", "Democratic"), market("2", "Republican")])[0]
    return bot, fake, arb.ArbOrder(b, "no", 100, (.38, .61), 99.0)


def test_live_clean_fill(monkeypatch, tmp_path):
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 100]])
    assert bot.execute(order)
    assert len(fake.calls) == 1 and not fake.cancelled and not bot.frozen
    assert bot.spent == pytest.approx(99.0)


def test_live_lopsided_fill_is_repaired(monkeypatch, tmp_path):
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40], [60]])
    bot.execute(order)
    assert fake.cancelled == [1]  # resting remainder of leg 2 pulled
    repair = fake.calls[1]
    assert repair == [{"exchangeId": "e2", "side": "no", "quantity": 60, "price": pytest.approx(.63)}]
    assert not bot.frozen


def test_live_unrepairable_basket_is_frozen(monkeypatch, tmp_path):
    bot, fake, order = live_bot(monkeypatch, tmp_path, [[100, 40], [10]])
    bot.execute(order)
    assert bot.frozen == {"S-TX"}
