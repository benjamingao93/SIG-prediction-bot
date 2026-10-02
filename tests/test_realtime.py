import time
from datetime import datetime, timezone

import pytest

from sigbot.api.realtime import BookStore, MarketFeed, _epoch


def raw(ex, seq, at="2026-10-02T03:25:48.4001404+00:00", bid=.5, ask=.51, expiry=None):
    return {"exchangeId": ex, "asOf": None if seq is None else {"sequence": seq, "at": at},
            "nextExpiryAt": expiry, "bids": [{"price": bid, "quantity": 10}], "asks": [{"price": ask, "quantity": 10}]}


def batch(rev, prev, books=(), **extra):
    return {"event": "market_batch", "payload": {"delivery": {"revision": rev, "previousRevision": prev},
                                                 "books": list(books), "trades": [], **extra}}


def test_epoch_parses_seven_fractional_digits():
    whole = datetime(2026, 10, 2, 3, 25, 48, tzinfo=timezone.utc).timestamp()
    assert _epoch("2026-10-02T03:25:48.4001404+00:00") == pytest.approx(whole + .400140)
    assert _epoch("2026-10-02T03:25:58Z") == pytest.approx(whole + 10)
    assert _epoch(None) is None and _epoch("garbage") is None


def test_book_age_comes_from_engine_time():
    s = BookStore()
    s.apply("m", raw(1, 5, at="2026-10-02T03:25:48.4001404+00:00"))
    now = datetime(2026, 10, 2, 3, 25, 50, 400140, tzinfo=timezone.utc).timestamp()
    assert s.age("1", now) == pytest.approx(2.0)


def test_store_applies_only_newer_versions():
    s = BookStore()
    assert s.apply("m", raw(1067, 10, bid=.50))
    assert not s.apply("m", raw(1067, 9, bid=.40))  # older
    assert not s.apply("m", raw(1067, 10, bid=.40))  # same version
    assert s.apply("m", raw(1067, 11, bid=.45))
    assert s.books["1067"].best_bid == .45 and s.market_of["1067"] == "m"  # int id stored as str


def test_unversioned_rest_book_applies_and_any_push_replaces_it():
    s = BookStore()
    assert s.apply("m", raw(1, None, bid=.3), authoritative=True)
    assert s.apply("m", raw(1, 5, bid=.4))
    assert not s.apply("m", raw(1, None, bid=.2))  # unversioned push: ignored


def test_expired_books_queue_their_market():
    s = BookStore()
    s.apply("m1", raw(1, 5, expiry="2026-10-02T03:25:58+00:00"))
    s.apply("m2", raw(2, 5, expiry="2099-01-01T00:00:00+00:00"))
    assert s.expired_markets(_epoch("2026-10-02T03:26:00+00:00")) == {"m1"}
    assert s.expired_markets(_epoch("2026-10-02T03:26:00+00:00")) == set()  # only once


def test_feed_dedupes_detects_gaps_and_still_applies_books():
    f = MarketFeed(BookStore())
    assert f.handle("m", batch(10, 9, [raw(1, 100)])) == {"1"}
    # duplicate revision: arrays ignored, but a newer book still applies
    assert f.handle("m", batch(10, 9, [raw(1, 101)])) == {"1"}
    assert f.stats["duplicates"] == 1 and not f.need_resync
    f.handle("m", batch(12, 10))  # contiguous: covers 11-12
    assert not f.need_resync
    f.handle("m", batch(20, 15))  # 13-15 missed
    assert f.need_resync == {"m"} and f.stats["gaps"] == 1 and f.last_rev["m"] == 20


def test_resync_required_and_unsequenced_trades_force_a_reload():
    f = MarketFeed(BookStore())
    f.handle("a", {"resyncRequired": True, "delivery": {"revision": 5, "previousRevision": 4}})  # compact form
    assert "a" in f.need_resync and f.last_rev["a"] == 5
    f.handle("b", batch(3, 2, trades=[{"sequence": None}]))
    assert "b" in f.need_resync
