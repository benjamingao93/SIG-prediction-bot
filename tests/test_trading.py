from pathlib import Path

import pytest

from sigbot.api.models import Level, OrderBook
from sigbot.api.orders import round_to_tick
from sigbot.config import RiskLimits
from sigbot.models.base import Estimate
from sigbot.trading import signals, sizing
from sigbot.trading.risk import RiskManager

ASKS = [Level(.61, 100), Level(.62, 300), Level(.64, 500), Level(.67, 200), Level(.70, 1000)]


def book(bids=(), asks=ASKS):
    return OrderBook("ex1", "m1", list(bids), list(asks))


def test_book_walk_matches_worked_example():
    # p=.668: take .61/.62/.64, stop before .67
    sig = signals.evaluate(Estimate(.668, 0.0), book(), min_edge=0.0, uncertainty_mult=0)
    assert sig.side == "yes"
    assert sig.limit_price == .64
    assert sig.quantity == 900
    assert sig.avg_price == pytest.approx((.61 * 100 + .62 * 300 + .64 * 500) / 900)


def test_threshold_cuts_deeper_levels():
    # min_edge .03 → only buy at or below .638
    sig = signals.evaluate(Estimate(.668, 0.0), book(), min_edge=0.03, uncertainty_mult=0)
    assert sig.limit_price == .62 and sig.quantity == 400


def test_uncertainty_widens_threshold():
    assert signals.evaluate(Estimate(.668, 0.06), book(), min_edge=0.0) is None


def test_no_side_hits_yes_bids():
    # YES bids at .52 → NO buyable at .48. If p_yes = .40, NO worth .60 → buy NO.
    b = book(bids=[Level(.52, 200), Level(.50, 100)], asks=[Level(.55, 100)])
    sig = signals.evaluate(Estimate(.40, 0.0), b, min_edge=0.03)
    assert sig.side == "no"
    assert sig.limit_price == pytest.approx(.50)  # NO prices .48, .50 both clear
    assert sig.quantity == 300


def test_no_trade_when_fair():
    b = book(bids=[Level(.60, 100)], asks=[Level(.62, 100)])
    assert signals.evaluate(Estimate(.61, 0.0), b, min_edge=0.01) is None


def test_kelly_worked_example():
    assert sizing.kelly_fraction(.668, .60) == pytest.approx(.17)
    # 0.25 Kelly on 100k → 4250 SUSQies stake → 7083 shares at .60
    assert sizing.kelly_shares(.668, .60, 100_000, 0.25) == 7083
    assert sizing.kelly_fraction(.5, .6) == 0


@pytest.mark.parametrize("price,action,expected", [
    (.613, "buy", .61), (.613, "sell", .615), (.61, "buy", .61), (.61, "sell", .61),
    (.001, "buy", .005), (.999, "sell", .995),
])
def test_round_to_tick(price, action, expected):
    assert round_to_tick(price, action) == pytest.approx(expected)


def test_risk_caps_and_kill_switch(tmp_path: Path):
    limits = RiskLimits(max_position=1000, max_market_exposure=1500, max_group_exposure=2000, max_order_cost=800)
    rm = RiskManager(limits, tmp_path / "KILL", starting_equity=10_000)
    assert rm.check("e", "m", "g", cash=5000, equity=10_000).allowed_cost == 800
    rm.add("e", "m", "g", 700)
    d = rm.check("e", "m", "g", cash=5000, equity=10_000)
    assert d.allowed_cost == 300 and "position" in d.reason
    rm.add("e2", "m2", "g", 1200)
    assert rm.check("e3", "m3", "g", cash=5000, equity=10_000).allowed_cost == 100  # group cap
    (tmp_path / "KILL").touch()
    assert rm.check("e3", "m3", "other", cash=5000, equity=10_000).allowed_cost == 0


def test_loss_stop(tmp_path: Path):
    rm = RiskManager(RiskLimits(daily_loss_stop=1000), tmp_path / "KILL", starting_equity=10_000)
    assert rm.check("e", "m", "g", cash=5000, equity=8_900).allowed_cost == 0
