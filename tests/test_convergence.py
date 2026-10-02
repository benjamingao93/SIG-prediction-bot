import time
from types import SimpleNamespace

import pytest

from sigbot.config import DirectionalConfig
from sigbot.convergence import episodes, summary
from sigbot.data.external.races import Race
from sigbot.fairvalue_service import FairValueService
from sigbot.trading import arb
from sigbot.api.models import Exchange, Market


def row(ts, edge, price, fair, required=0.03, agree=1, race="S-RI", view="D"):
    return {"ts": f"2026-10-02T{ts}:00+00:00", "race": race, "view": view, "price": price, "fair": fair,
            "edge": edge, "required": required, "agree": agree}


def test_episode_closed_by_sig_repricing():
    rows = [row("10:00", .01, .97, .98),  # below the bar: no episode yet
            row("11:00", .06, .93, .99),  # opens
            row("12:00", .03, .96, .99),  # halved after 1 h
            row("13:00", .004, .986, .99)]  # closed after 2 h, all of it SIG's price
    (e,) = episodes(rows)
    assert e.edge0 == pytest.approx(.06) and e.half == pytest.approx(1) and e.closed == pytest.approx(2)
    assert e.by_price == pytest.approx(1.0)
    assert "closed within  6 h: 1 of 1" in summary([e])


def test_episode_closed_by_fair_value_moving_and_reopening():
    rows = [row("11:00", .06, .93, .99), row("12:00", .0, .93, .93),  # Kalshi came round to SIG
            row("13:00", .05, .93, .98)]  # a new episode
    eps = episodes(rows)
    assert len(eps) == 2 and eps[0].by_price == pytest.approx(0.0)


def test_disagreeing_sources_never_open_an_episode():
    assert episodes([row("11:00", .06, .93, .99, agree=0)]) == []


def mkt(mid, party, race):
    return Market(mid, f"Will the {party} Party win the {race}?", "open", None, None, [], False,
                  [Exchange(f"e{mid}", "YES", None)])


def test_service_ignores_stale_kalshi(tmp_path):
    s = SimpleNamespace(dir=DirectionalConfig(), races_path=tmp_path / "r.csv", kalshi_map_path=tmp_path / "k.csv",
                        db_path=tmp_path / "t.db")
    svc = FairValueService.__new__(FairValueService)
    svc.s, svc.cfg = s, s.dir
    svc.races = {"S-RI": Race("S-RI", "senate", 8, rating_p_d=0.97, rating_n=7, rating_spread=0.0)}
    svc.kalshi = {"S-RI": {"D": {"bid": .985, "ask": .995, "open_interest": 5000},
                           "R": {"bid": .006, "ask": .014, "open_interest": 5000}}}
    baskets = arb.build_baskets([mkt("1", "Democratic", "Rhode Island Senate"), mkt("2", "Republican", "Rhode Island Senate")])
    sig = {"e1": (.93, .94), "e2": (.07, .08)}
    svc.updated = time.time()
    fresh = {e.view: e for e in svc.edges(baskets, sig)}
    assert fresh["D"].fv.sources == "kalshi+ratings"
    svc.updated = time.time() - 3600  # an hour old
    stale = {e.view: e for e in svc.edges(baskets, sig)}
    assert stale["D"].fv.sources == "ratings"  # Kalshi dropped, ratings alone remain
