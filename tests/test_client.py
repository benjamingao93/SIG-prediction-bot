import httpx
import pytest

from sigbot.api.client import RateLimiter, SigAPIError, SigClient


def make(handler, **kw):
    sleeps = []
    c = SigClient("k", "https://x/api/v1", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return c, sleeps


def test_auth_header_and_json():
    def h(req):
        assert req.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"ok": True})
    c, _ = make(h)
    assert c.get("/account") == {"ok": True}


def test_retries_rate_limited_with_retry_after():
    calls = []
    def h(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"},
                                  json={"error": {"code": "RATE_LIMITED", "message": "slow"}})
        return httpx.Response(200, json={"ok": 1})
    c, sleeps = make(h)
    assert c.get("/x") == {"ok": 1}
    assert sleeps == [7.0]


def test_order_retry_resends_same_idempotency_key():
    bodies = []
    def h(req):
        bodies.append(req.content)
        if len(bodies) == 1:
            return httpx.Response(502, json={"error": {"code": "ORDER_STATUS_UNKNOWN", "message": "?"}})
        return httpx.Response(200, json={"orderId": 1})
    c, _ = make(h)
    c.post("/orders", {"idempotencyKey": "abc", "quantity": 1})
    assert bodies[0] == bodies[1]


def test_non_retryable_raises_with_code():
    c, _ = make(lambda r: httpx.Response(403, json={"error": {"code": "TERMS_NOT_ACKNOWLEDGED", "message": "x"}}))
    with pytest.raises(SigAPIError) as e:
        c.get("/x")
    assert e.value.code == "TERMS_NOT_ACKNOWLEDGED"


def test_pagination():
    def h(req):
        if req.url.params.get("cursor") == "c2":
            return httpx.Response(200, json={"data": [3], "pagination": {"hasMore": False}})
        return httpx.Response(200, json={"data": [1, 2], "pagination": {"hasMore": True, "nextCursor": "c2"}})
    c, _ = make(h)
    assert list(c.paginate("/m")) == [1, 2, 3]


def test_rate_limiter_blocks_when_full():
    t = [0.0]
    slept = []
    def sleep(s):
        slept.append(s)
        t[0] += s
    rl = RateLimiter(2, clock=lambda: t[0], sleep=sleep)
    rl.acquire(); rl.acquire()
    rl.acquire()  # must wait ~60s
    assert slept and slept[0] == pytest.approx(60.01)
