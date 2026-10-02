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


# ---- FeedRunner: watchlist, trust, verification ----

import asyncio
from types import SimpleNamespace

from sigbot.api.realtime import FeedRunner


class FakeChannel:
    def __init__(self, topic, joins):
        self.topic, self.joins = topic, joins

    def on_broadcast(self, event, cb):
        self.cb = cb

    async def subscribe(self, cb):
        cb("RealtimeSubscribeStates.SUBSCRIBED" if self.joins else "RealtimeSubscribeStates.TIMED_OUT")


class FakeSocket:
    def __init__(self, failing=()):
        self.failing, self.removed = set(failing), []

    def channel(self, topic, params):
        return FakeChannel(topic, topic.rsplit(":", 1)[-1] not in self.failing)

    async def remove_channel(self, ch):
        self.removed.append(ch.topic)


def runner(**kw):
    client = SimpleNamespace(reads=SimpleNamespace(available=100), get=kw.pop("get", None))
    return FeedRunner(client, "t", **kw)


def test_usable_needs_health_subscription_no_pending_reload_and_no_expiry():
    r = runner()
    r.store.apply("m", raw(1, 5, expiry="2099-01-01T00:00:00+00:00"))
    assert not r.usable("1")  # not healthy yet
    r.healthy, r.subscribed = True, {"m"}
    assert r.usable("1")
    r.feed.need_resync.add("m")
    assert not r.usable("1")
    r.feed.need_resync.clear()
    r.store.expiry["1"] = time.time() - 1  # a resting order expired silently
    assert not r.usable("1")


def test_reconcile_joins_new_markets_leaves_old_and_backs_off_failures(monkeypatch):
    monkeypatch.setattr("sigbot.api.realtime.JOIN_WAIT", 0.05)
    r, sb = runner(market_ids=["a", "b", "x"]), FakeSocket(failing={"x"})
    asyncio.run(r._reconcile(sb))
    assert r.subscribed == {"a", "b"} and set(r.joined) == {"a", "b"}
    assert r.feed.need_resync == {"a", "b"}  # initial books come from REST
    assert "x" in r._join_after  # retried later, not every loop
    r.store.apply("a", raw(1, 5))
    r.set_watch(["b", "c"])
    asyncio.run(r._reconcile(sb))
    assert set(r.joined) == {"b", "c"} and "a" not in r.subscribed
    assert "1" not in r.store.books  # a left market's books are dropped
    assert any(t.endswith(":a") for t in sb.removed)


def test_verify_reloads_a_lagging_book():
    lagging = {"asOf": {"sequence": 9, "at": "2026-10-02T03:26:00+00:00"}}
    r = runner(get=lambda path, **kw: lagging)
    r.store.apply("m", raw(1, 5, at="2026-10-02T03:25:48+00:00"))
    r.healthy, r.subscribed = True, {"m"}
    assert r._verify_one() is True and "m" in r.feed.need_resync  # 12 s behind
    r.feed.need_resync.clear()
    r.store.apply("m", raw(1, 9, at="2026-10-02T03:26:00+00:00"))
    assert r._verify_one() is False


def test_account_channel_signals_fills_and_flags_gaps():
    r = runner()
    woke = []
    r.on_fills = lambda: woke.append(1)
    r.healthy, r.user_subscribed, r.user_need_poll = True, True, False

    def acct(rev, prev, fills=(), **extra):
        return {"payload": {"delivery": {"revision": rev, "previousRevision": prev}, "fills": list(fills), **extra}}
    r._on_account(acct(5, 4, [{"orderId": 1}]))
    assert r.pop_fills() == [{"orderId": 1}] and woke == [1] and r.user_trusted()
    r._on_account(acct(5, 4, [{"orderId": 1}]))  # duplicate batch
    assert r.pop_fills() == [] and woke == [1]
    r._on_account(acct(9, 7))  # 6-7 missed
    assert not r.user_trusted() and r.user_need_poll


def test_call_patiently_retries_slow_exchange_but_not_bad_requests():
    from sigbot.api.client import SigAPIError, call_patiently
    seen, slept = [], []
    client = SimpleNamespace(patient=lambda t: __import__("contextlib").nullcontext())

    def flaky():
        seen.append(1)
        if len(seen) < 3:
            raise SigAPIError(0, "TRANSPORT_ERROR", "timed out")
        return "ok"
    assert call_patiently(client, flaky, "x", sleep=slept.append) == "ok" and slept == [2.0, 4.0]

    def bad():
        raise SigAPIError(400, "VALIDATION_ERROR", "nope")
    with pytest.raises(SigAPIError):
        call_patiently(client, bad, "x", sleep=slept.append)
