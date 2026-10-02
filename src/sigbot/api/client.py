"""HTTP transport for the Super Market API: auth, per-minute rate budgets, retries.

Account limits (shared across every key and process): 100 reads + 30 writes per minute.
GET/HEAD are reads; everything else is a write.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, Iterator, Optional, TypeVar

import httpx

log = logging.getLogger(__name__)
T = TypeVar("T")

# Codes the API documents as safe to retry. ORDER_STATUS_UNKNOWN is only safe because
# order POSTs carry an idempotencyKey and we resend the identical body.
RETRYABLE_CODES = {
    "RATE_LIMITED",
    "TX_CONFLICT",
    "SERVICE_UNAVAILABLE",
    "ORDER_STATUS_UNKNOWN",
    "REQUEST_IN_FLIGHT",
}


class SigAPIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class RateLimiter:
    """Sliding 60-second window. acquire() blocks until a slot is free."""

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._hits: Deque[float] = deque()
        self._lock = threading.Lock()  # the arb bot reads books from several threads

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._hits and now - self._hits[0] >= 60.0:
                    self._hits.popleft()
                if len(self._hits) < self.per_minute:
                    self._hits.append(now)
                    return
                wait = 60.0 - (now - self._hits[0]) + 0.01
            self._sleep(wait)

    @property
    def available(self) -> int:
        """Calls that can go out right now without waiting."""
        return self.per_minute - self.used

    @property
    def used(self) -> int:
        now = self._clock()
        return sum(1 for t in self._hits if now - t < 60.0)


class SigClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        read_budget: int = 80,
        write_budget: int = 25,
        timeout: float = 15.0,
        max_retries: int = 4,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._sleep = sleep
        self.reads = RateLimiter(read_budget, sleep=sleep)
        self.writes = RateLimiter(write_budget, sleep=sleep)
        self.max_retries = max_retries
        self._local = threading.local()  # per-thread timeout override (see patient())

    @contextmanager
    def patient(self, timeout: float):
        """Longer per-request timeout for this thread only, e.g. at startup on a slow exchange."""
        prev = getattr(self._local, "timeout", None)
        self._local.timeout = timeout
        try:
            yield self
        finally:
            self._local.timeout = prev

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=_clean(params))

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("POST", path, json=body or {})

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def paginate(self, path: str, key: str = "data", **params: Any) -> Iterator[Any]:
        cursor = None
        while True:
            page = self.get(path, cursor=cursor, **params)
            yield from page.get(key, [])
            pag = page.get("pagination") or {}
            cursor = pag.get("nextCursor")
            if not pag.get("hasMore") or not cursor:
                return

    def _request(self, method: str, path: str, params=None, json=None) -> Any:
        limiter = self.reads if method in ("GET", "HEAD") else self.writes
        attempt = 0
        while True:
            limiter.acquire()
            try:
                override = getattr(self._local, "timeout", None)
                resp = self._http.request(method, path, params=params, json=json,
                                          timeout=override if override else httpx.USE_CLIENT_DEFAULT)
            except httpx.TransportError as e:
                if attempt >= self.max_retries:
                    raise SigAPIError(0, "TRANSPORT_ERROR", str(e)) from e
                self._sleep(_backoff(attempt))
                attempt += 1
                continue

            if resp.status_code < 400:
                return resp.json() if resp.content else None

            err = _parse_error(resp)
            if err.code in RETRYABLE_CODES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else _backoff(attempt)
                self._sleep(wait)
                attempt += 1
                continue
            raise err


def _backoff(attempt: int) -> float:
    return min(2.0 ** attempt, 30.0)


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _parse_error(resp: httpx.Response) -> SigAPIError:
    try:
        e = resp.json().get("error", {})
        return SigAPIError(resp.status_code, e.get("code", "UNKNOWN"), e.get("message", ""), e.get("details"))
    except ValueError:
        return SigAPIError(resp.status_code, "UNKNOWN", resp.text[:200])


# Failures that mean the exchange is slow or struggling, not that the request is wrong.
TRANSIENT_CODES = RETRYABLE_CODES | {"TRANSPORT_ERROR", "INTERNAL_ERROR", "DATABASE_ERROR"}


def call_patiently(client: SigClient, fn: Callable[[], T], what: str, timeout: float = 60.0,
                   max_backoff: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> T:
    """Run fn with long request timeouts, retrying for as long as the exchange is slow or
    erroring (seen live: startup reads taking 14-36 s). Real client errors still raise."""
    backoff = 2.0
    with client.patient(timeout):
        while True:
            try:
                return fn()
            except SigAPIError as e:
                if e.code not in TRANSIENT_CODES and e.status < 500:
                    raise
                log.warning("%s: %s — exchange slow or failing, retrying in %.0fs", what, e, backoff)
                sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
