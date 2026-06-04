"""Per-IP token bucket rate limiter. Lightweight, in-memory, no dependencies.

This is a local-first defence-in-depth layer: the sidecar binds to 127.0.0.1
by default, so there's no public threat model, but a runaway UI loop or an
operator script that accidentally hammers `/api/db/import` in a `for` loop
will burn DeepSeek tokens / disk IO before they notice.

Three bucket presets are exposed at module level so each endpoint can pick
the right severity. Buckets are keyed by client host (request.client.host)
which is good enough for local — the kernel makes IP spoofing on loopback
impractical.

Buckets are in-memory only. They reset on sidecar restart; that's fine —
the limiter is for accidental abuse, not adversaries, and a restart is the
admin's escape hatch.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock

from fastapi import HTTPException, Request


@dataclass
class Bucket:
    """A single token bucket's mutable state.

    `tokens` is float because refill is continuous. `last_refill` is a
    monotonic timestamp so wall-clock jumps (NTP, sleep/wake) don't
    accidentally credit a year's worth of tokens.
    """
    tokens: float
    last_refill: float


class TokenBucket:
    """Token bucket per-key (IP). Refills at `rate` tokens/sec, max `capacity`.

    `allow(key, cost)` returns `(allowed, retry_after_seconds)`. When
    denied, the caller raises 429 with `retry_after` in the `Retry-After`
    header (per RFC 7231 §7.1.3 — well-behaved clients will back off).
    """

    def __init__(self, capacity: int, rate_per_sec: float):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.capacity = capacity
        self.rate = rate_per_sec
        # `defaultdict` factory captures the current time per fresh bucket
        # so a brand-new key starts at full capacity (i.e. no penalty for
        # the very first request from a previously-unseen client).
        self.buckets: dict[str, Bucket] = defaultdict(
            lambda: Bucket(float(capacity), time.monotonic())
        )
        self.lock = Lock()

    def allow(self, key: str, cost: int = 1) -> tuple[bool, float]:
        """Atomically check + consume `cost` tokens for `key`.

        Returns (True, 0.0) on success, (False, retry_after_seconds) on
        denial. The retry_after is computed from the deficit and refill
        rate, so it's a precise lower bound — sleeping for that long
        guarantees the next request succeeds.
        """
        if cost < 1:
            cost = 1
        now = time.monotonic()
        with self.lock:
            b = self.buckets[key]
            # Clamp to >= 0: the `defaultdict` factory samples `time.monotonic()`
            # AFTER `now` for a previously-unseen key, so `now - b.last_refill`
            # would be negative on a bucket's very first use and (wrongly) drop it
            # below capacity — denying the first request from a fresh client.
            # The clamp restores the "no penalty for the first request" contract
            # and hardens against any monotonic anomaly.
            elapsed = max(0.0, now - b.last_refill)
            # Cap at `capacity` so a long idle period doesn't translate
            # into an unlimited burst credit later.
            b.tokens = min(float(self.capacity), b.tokens + elapsed * self.rate)
            b.last_refill = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True, 0.0
            deficit = cost - b.tokens
            retry_after = deficit / self.rate
            return False, retry_after

    def reset(self) -> None:
        """Drop all per-key state. Test-only helper."""
        with self.lock:
            self.buckets.clear()


# ── Bucket presets ──────────────────────────────────────────────────────────
#
# Generous limits intentional: catchem is a local app, so the goal is to
# stop accidental loops (e.g. a misconfigured cron that hits /api/search
# every 10ms), not throttle real users. Numbers chosen so a human pressing
# the UI as fast as they can will never hit the cap.
#
#   DEFAULT_BUCKET  — 60 req/min   (1 req/sec sustained, 60 burst)
#   SEARCH_BUCKET   — 30 req/min   (search is moderate cost; palette debounce
#                                    is 200ms so this is ~6x faster than
#                                    realistic user typing)
#   DB_IMPORT_BUCKET — 6 req/min   (heavy disk + backup file; combined with
#                                    cost=5 below it's effectively 1/min)
#
# `rate_per_sec = capacity / 60` mirrors "X requests per minute" semantics
# while still permitting a small burst (the full bucket) on a cold start.
DEFAULT_BUCKET = TokenBucket(capacity=60, rate_per_sec=60 / 60)
SEARCH_BUCKET = TokenBucket(capacity=30, rate_per_sec=30 / 60)
DB_IMPORT_BUCKET = TokenBucket(capacity=6, rate_per_sec=6 / 60)


def client_key(request: Request) -> str:
    """Extract a stable client key for bucket lookup.

    `request.client.host` is the peer IP from the TCP layer — unforgeable
    over loopback. We don't trust `X-Forwarded-For` because the sidecar
    is never behind a reverse proxy in the documented deploy model. If
    `client` is missing (test clients without an explicit host), fall back
    to a constant so the limiter still functions; the constant means all
    tests share one bucket, which is what `reset()` in fixtures expects.
    """
    return request.client.host if request.client else "unknown"


def check_rate(request: Request, bucket: TokenBucket, cost: int = 1) -> None:
    """Raises 429 with Retry-After if the caller has exhausted the bucket.

    Dependency-injectable from FastAPI handlers via:

        def rate_limit_search(request: Request):
            check_rate(request, SEARCH_BUCKET)

        @app.get("/api/search", dependencies=[Depends(rate_limit_search)])

    The dependency wrapper layer is necessary because FastAPI's `Depends`
    can't bind extra args directly into a function — it inspects the
    signature, so each per-bucket wrapper is a thin alias.
    """
    allowed, retry_after = bucket.allow(client_key(request), cost)
    if not allowed:
        # Round up so Retry-After is never 0 (which would invite an
        # immediate retry the limiter would reject again). The detail
        # string contains the verbatim string "Rate limit" — the SPA's
        # ErrorBox checks for that substring to swap in a friendlier
        # message.
        retry_seconds_int = int(retry_after) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after:.1f}s.",
            headers={"Retry-After": str(retry_seconds_int)},
        )


def reset_all_buckets() -> None:
    """Drop in-memory state for every preset bucket. Test-only helper.

    Called from pytest fixtures to keep tests independent — without this,
    bucket state leaks across the test module and a later test sees the
    deficit from an earlier one.
    """
    DEFAULT_BUCKET.reset()
    SEARCH_BUCKET.reset()
    DB_IMPORT_BUCKET.reset()
