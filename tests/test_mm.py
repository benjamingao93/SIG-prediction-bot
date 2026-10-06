from pathlib import Path
from types import SimpleNamespace

import pytest

from sigbot.config import DirectionalConfig, MMConfig
from sigbot.data.db import DB
from sigbot.data.external.races import Race
from sigbot.market_maker import MarketMaker
from sigbot.models.fairvalue import edges, fair_value
from sigbot.trading.mm import Inventory, capture, quote_prices

from test_directional import BASKETS, kq

# ---- pricing ----


def test_quotes_step_inside_the_book_but_keep_their_distance_from_fair():
    assert quote_prices(.60, .55, .66, 0, .01, 1000, .01) == (.555, .655)  # a tick inside both sides
    assert quote_prices(.60, .585, .615, 0, .01, 1000, .01) == (.59, .61)  # tight book: sit at fair ± edge


def test_quotes_never_cross_the_book():
    b, a = quote_prices(.60, .45, .50, 0, .01, 1000, .01)  # the book is far below fair
    assert b == .455 and a == .61  # a tick over their bid, never up into their ask
    b, a = quote_prices(.60, .58, .59, 0, .01, 1000, .01)  # fair bid would cross their .59 ask
    assert b == .585 and a == .61


def test_inventory_skews_quotes_and_caps_the_side_that_adds():
    b0, a0 = quote_prices(.60, .50, .70, 0, .01, 1000, .02)
    b1, a1 = quote_prices(.60, .50, .70, 500, .01, 1000, .02)  # long YES: offer keener to sell
    assert b1 <= b0 and a1 < a0 and a1 >= .60 - .01 + .01  # still above fair − skew·½ + edge
    b, a = quote_prices(.60, .50, .70, 1000, .01, 1000, .02)  # full: no more buying YES
    assert b is None and a is not None
    b, a = quote_prices(.60, .50, .70, -1000, .01, 1000, .02)
    assert b is not None and a is None


def test_capture_needs_both_quotes_at_the_top():
    assert capture(.60, .55, .66, .01) == pytest.approx(.10)
    assert capture(.60, .595, .66, .01) == 0  # our bid would sit behind theirs


def test_a_round_trip_cancels_the_pair_and_banks_the_spread():
    inv = Inventory()
    assert inv.fill("yes", 200, .555) == 0 and inv.net == 200
    assert inv.fill("no", 150, 1 - .655) == pytest.approx(150 * .10)  # 150 pairs pay 1 each
    assert inv.net == 50 and inv.yes_cost == pytest.approx(50 * .555)
    assert inv.unrealized(.60) == pytest.approx(50 * (.60 - .555))


# ---- the driver (paper) ----


def make(tmp_path, held=()):
    dcfg = DirectionalConfig()
    ri = fair_value(Race("S-RI", "senate", 8, rating_p_d=.6, rating_n=7, rating_spread=0), kq((.59, .61), (.39, .41)), dcfg)
    me = fair_value(Race("S-ME", "senate", 4, rating_p_d=.5, rating_n=10, rating_spread=0), kq((.49, .51), (.49, .51)), dcfg)
    sig = {"e1": (.50, .70), "e2": (.30, .50), "e3": (.40, .60), "e4": (.40, .60)}
    bot = SimpleNamespace(
        s=SimpleNamespace(mm=MMConfig(enabled=True, markets=5, size=100, writes_per_cycle=10),
                          kill_switch=Path(tmp_path / "KILL")),
        db=DB(tmp_path / "t.db"), baskets=BASKETS, repairs={}, fair=SimpleNamespace(fresh=lambda: True),
        _edges=edges(ri, "e1", "e2", sig, dcfg) + edges(me, "e3", "e4", sig, dcfg))
    for race in held:
        bot.db.save_dir_position({"mode": "live", "race": race, "view": "D", "exchange_id": "x", "qty": 1,
                                  "cost": 1.0, "entry_price": 1.0, "halved": 0, "realized": 0.0})
    return MarketMaker(bot), bot, sig


def test_quotes_one_market_per_race_and_skips_directional_races(tmp_path):
    m, bot, sig = make(tmp_path, held=("S-ME",))
    m.step(sig, {ex: .6 for ex in sig})
    assert len(m.chosen) == 1 and m.chosen[0] in ("e1", "e2")  # RI only; Maine is held directionally


def test_paper_round_trip_and_state_survive_a_restart(tmp_path):
    m, bot, sig = make(tmp_path, held=("S-ME",))
    lasts = {ex: .6 for ex in sig}
    m.step(sig, lasts)
    ex = m.chosen[0]
    bid = m.quotes[ex]["bid"]
    m.step(sig, {**lasts, ex: bid})  # a trade prints at our bid: we buy YES
    ask = m.quotes[ex]["ask"]  # now long YES, the ask has leaned in a little to unwind
    m.step(sig, {**lasts, ex: ask})  # a trade at our ask: we buy NO, the pair cancels
    inv = m.inv[ex]
    assert inv.pairs == 100 and inv.net == 0 and inv.realized == pytest.approx(100 * (ask - bid))
    assert ask - bid > 0
    again = MarketMaker(bot)
    assert again.inv[ex].realized == pytest.approx(inv.realized)


def test_kill_switch_or_stale_fair_value_pulls_quotes(tmp_path):
    m, bot, sig = make(tmp_path)
    m.step(sig, {ex: .6 for ex in sig})
    assert m.quotes
    bot.fair = SimpleNamespace(fresh=lambda: False)
    m.step(sig, {ex: .6 for ex in sig})
    assert not m.quotes
