"""Rate-limit contract tests.

Pins per-bucket behaviour at the API surface — not just the unit-level
TokenBucket, because the real bug surface is "did we wire it to the
endpoint correctly?" not "does my refill maths work?". Each test seeds
its own client and resets the bucket state in setup to keep tests
independent of order.

Six tests:
  1. 30 search requests succeed, the 31st returns 429 with a
     `Retry-After` header.
  2. After waiting ~2s the bucket refills enough for one more search.
  3. Two distinct client IPs each get their own full bucket
     (no cross-contamination).
  4. The 429 detail body contains the verbatim "Rate limit" substring
     the SPA's ErrorBox is looking for.
  5. /healthz is NOT rate-limited (must always answer liveness pings).
  6. /api/db/import burns 5 tokens per call => ~one allowed per minute
     given a capacity of 6.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem import rate_limit as rl
from catchem.api import create_app
from catchem.rate_limit import (
    DB_IMPORT_BUCKET,
    DEFAULT_BUCKET,
    SEARCH_BUCKET,
    TokenBucket,
    reset_all_buckets,
)
from catchem.settings import load_settings, reload_settings

# Tiny but valid SQLite file: header + an empty database body. Used by
# the db_import test so we exercise rate-limit logic, not validation.
_SQLITE_MAGIC = b"SQLite format 3\x00"


@pytest.fixture(autouse=True)
def _clean_buckets() -> None:
    """Reset bucket state between tests so order doesn't matter."""
    reset_all_buckets()
    yield
    reset_all_buckets()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_search_bucket_caps_at_capacity(client: TestClient) -> None:
    """30 quick searches succeed; the 31st must return 429 + Retry-After."""
    # All 30 within the burst capacity. We use a query that the seeded
    # corpus doesn't match (empty buckets) so even a 200-without-data
    # path is exercised — the limiter must fire before the handler body.
    for i in range(30):
        r = client.get("/api/search?q=zzz_no_match")
        assert r.status_code == 200, f"req {i} failed: {r.status_code} {r.text[:120]}"
    r = client.get("/api/search?q=zzz_no_match")
    assert r.status_code == 429, r.text
    # Retry-After must be a positive integer string per RFC 7231.
    retry_after = r.headers.get("Retry-After")
    assert retry_after is not None, "Retry-After header missing"
    assert int(retry_after) >= 1, f"Retry-After should be >= 1 sec, got {retry_after!r}"


def test_search_bucket_refills_after_wait(client: TestClient) -> None:
    """SEARCH_BUCKET refills at 30 tokens/60s = 0.5 tok/s.

    After exhausting the bucket and sleeping 2.2 seconds, at least one
    token is back (0.5 * 2.2 = 1.1 tokens), so the next call succeeds.
    """
    for _ in range(30):
        r = client.get("/api/search?q=zzz_refill_marker")
        assert r.status_code == 200
    r = client.get("/api/search?q=zzz_refill_marker")
    assert r.status_code == 429
    time.sleep(2.2)
    r = client.get("/api/search?q=zzz_refill_marker")
    assert r.status_code == 200, f"refilled token rejected: {r.status_code} {r.text[:120]}"


def test_distinct_ips_have_independent_buckets() -> None:
    """Bucket key is `request.client.host` — two IPs ≡ two buckets.

    Drives `SEARCH_BUCKET.allow(...)` directly with synthetic keys
    instead of going through HTTP, because TestClient can't change its
    peer host between calls. The route wiring is already covered by
    `test_search_bucket_caps_at_capacity`; this test pins the per-key
    isolation that the wiring depends on.
    """
    # Burn through "127.0.0.1"'s budget entirely.
    for _ in range(SEARCH_BUCKET.capacity):
        allowed, _ = SEARCH_BUCKET.allow("127.0.0.1")
        assert allowed
    # 127.0.0.1 should now be capped.
    allowed_a, retry_a = SEARCH_BUCKET.allow("127.0.0.1")
    assert not allowed_a
    assert retry_a > 0
    # A new IP starts at full capacity — independent bucket.
    allowed_b, retry_b = SEARCH_BUCKET.allow("10.0.0.42")
    assert allowed_b
    assert retry_b == 0.0


def test_429_body_carries_rate_limit_marker(client: TestClient) -> None:
    """ErrorBox in the SPA checks `error.message.includes("Rate limit")`.

    Pinning the verbatim string here so a future refactor doesn't break
    the frontend's friendly-message swap silently.
    """
    for _ in range(30):
        client.get("/api/search?q=zz_marker_only")
    r = client.get("/api/search?q=zz_marker_only")
    assert r.status_code == 429
    body = r.json()
    assert "Rate limit" in body.get("detail", ""), f"missing marker in {body!r}"


def test_healthz_never_rate_limited(client: TestClient) -> None:
    """Liveness probe must answer every single time.

    Even after pummeling /api/search to deplete its bucket, /healthz must
    stay 200 — it's the only signal the Tauri shell uses to know the
    sidecar is alive, and a 429 there would manifest as an offline UI.
    """
    # Exhaust the search bucket entirely. This shouldn't matter for
    # /healthz, but we want to be sure no middleware applies a global
    # rate limit by accident.
    for _ in range(35):
        client.get("/api/search?q=health_check_seed")
    for _ in range(50):
        r = client.get("/healthz")
        assert r.status_code == 200, f"/healthz refused liveness ping: {r.status_code}"
        assert r.json() == {"status": "ok"}


def test_db_import_cost_5_throttles_quickly() -> None:
    """DB_IMPORT_BUCKET capacity is 6, cost per call is 5 => at most one
    call drains the bucket below the refill threshold.

    Drives the bucket directly because actually POSTing 200MB blobs
    twice in a test would be wasteful and slow.
    """
    # First "import" call passes (6 tokens available, costs 5).
    allowed, _ = DB_IMPORT_BUCKET.allow("test_client", cost=5)
    assert allowed
    # Second one with cost=5 must fail — only 1 token left, deficit 4.
    allowed_2, retry = DB_IMPORT_BUCKET.allow("test_client", cost=5)
    assert not allowed_2
    # 6 tokens / 60s = 0.1 tok/s. Need 4 more tokens => 40s retry-after.
    # Don't pin the exact number; just check the order of magnitude.
    assert retry > 10.0, f"retry_after too short to be useful: {retry}"

    # Sanity: DEFAULT_BUCKET still has independent state.
    allowed_default, _ = DEFAULT_BUCKET.allow("test_client", cost=1)
    assert allowed_default


# ── Reserved-for-CI smoke: never imported, just an explicit asserts on
# the bucket presets we expect to ship. Lets a future refactor that
# accidentally drops a bucket fail at test-collection time.
def test_bucket_preset_shape() -> None:
    assert DEFAULT_BUCKET.capacity == 60
    assert SEARCH_BUCKET.capacity == 30
    assert DB_IMPORT_BUCKET.capacity == 6
    # Rates are scaled "per minute" so capacity / 60 == rate.
    assert abs(DEFAULT_BUCKET.rate - 1.0) < 1e-9
    assert abs(SEARCH_BUCKET.rate - 0.5) < 1e-9
    assert abs(DB_IMPORT_BUCKET.rate - 0.1) < 1e-9


# ── Unit-level TokenBucket tests (controllable clock, no real sleeps) ─────────
#
# The HTTP-surface tests above prove the wiring; these pin the refill maths
# deterministically by monkeypatching `time.monotonic` so the bucket's notion
# of "now" advances exactly as far as we say — no flaky wall-clock sleeps.


class _FakeClock:
    """Advanceable monotonic clock for deterministic refill testing.

    `rate_limit` reads `time.monotonic()` at module scope (via `import time`),
    so monkeypatching `rate_limit.time.monotonic` redirects every bucket on the
    path to this controllable source.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(rl.time, "monotonic", clock)
    return clock


def test_bucket_blocks_over_limit_within_window(fake_clock: _FakeClock) -> None:
    """A full bucket allows exactly `capacity` requests in a frozen window.

    With the clock frozen, no tokens refill — so the (capacity+1)-th request
    must be denied, and the reported retry_after is a positive lower bound.
    """
    bucket = TokenBucket(capacity=5, rate_per_sec=1.0)
    for i in range(5):
        allowed, retry = bucket.allow("ip-A")
        assert allowed, f"request {i} should be under limit"
        assert retry == 0.0
    # Clock has NOT advanced => no refill => next request is over-limit.
    allowed, retry = bucket.allow("ip-A")
    assert not allowed
    # Deficit is 1 token at 1 tok/sec => retry_after == 1.0s exactly.
    assert retry == pytest.approx(1.0)


def test_bucket_resets_after_window_elapses(fake_clock: _FakeClock) -> None:
    """Advancing the clock past the window refills the bucket to capacity.

    Drains the bucket, advances well beyond the full-refill time, and asserts
    the bucket is usable again — and that idle time never over-credits past
    `capacity` (the burst-credit cap on line 78).
    """
    bucket = TokenBucket(capacity=3, rate_per_sec=1.0)
    for _ in range(3):
        assert bucket.allow("ip-B")[0]
    assert not bucket.allow("ip-B")[0]

    # Advance one second => exactly one token back => exactly one request.
    fake_clock.advance(1.0)
    assert bucket.allow("ip-B")[0]
    assert not bucket.allow("ip-B")[0]

    # Advance far beyond a full refill (100s ≫ 3 tokens worth). The cap must
    # hold: only `capacity` requests succeed, not 100.
    fake_clock.advance(100.0)
    succeeded = 0
    while bucket.allow("ip-B")[0]:
        succeeded += 1
        if succeeded > 50:  # safety valve against an uncapped refill bug
            break
    assert succeeded == 3, f"idle burst credit exceeded capacity: {succeeded}"


def test_bucket_partial_refill_is_proportional(fake_clock: _FakeClock) -> None:
    """Half a window of elapsed time credits half the tokens (continuous refill)."""
    bucket = TokenBucket(capacity=10, rate_per_sec=2.0)  # 10 cap, 2 tok/sec
    for _ in range(10):
        assert bucket.allow("ip-C")[0]
    assert not bucket.allow("ip-C")[0]
    # 2.5 seconds x 2 tok/sec = 5 tokens refilled => exactly 5 more allowed.
    fake_clock.advance(2.5)
    granted = sum(1 for _ in range(10) if bucket.allow("ip-C")[0])
    assert granted == 5, f"expected 5 refilled tokens, got {granted}"


def test_constructor_rejects_bad_capacity() -> None:
    """capacity < 1 is nonsensical (no request could ever pass) => ValueError."""
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        TokenBucket(capacity=0, rate_per_sec=1.0)


def test_constructor_rejects_non_positive_rate() -> None:
    """rate_per_sec <= 0 means the bucket would never refill => ValueError."""
    with pytest.raises(ValueError, match="rate_per_sec must be > 0"):
        TokenBucket(capacity=5, rate_per_sec=0.0)
    with pytest.raises(ValueError, match="rate_per_sec must be > 0"):
        TokenBucket(capacity=5, rate_per_sec=-1.0)


def test_cost_below_one_is_clamped_to_one(fake_clock: _FakeClock) -> None:
    """A cost < 1 is coerced to 1 so a zero/negative cost can't be free.

    Otherwise a caller passing cost=0 would consume nothing and could loop
    forever — exactly the accidental-abuse case the limiter exists to stop.
    """
    bucket = TokenBucket(capacity=2, rate_per_sec=1.0)
    assert bucket.allow("ip-D", cost=0)[0]   # clamped to 1 => 1 token left
    assert bucket.allow("ip-D", cost=-5)[0]  # clamped to 1 => 0 tokens left
    # Bucket is now empty; a third clamped call must be denied.
    allowed, retry = bucket.allow("ip-D", cost=0)
    assert not allowed
    assert retry > 0.0


def test_reset_restores_full_capacity(fake_clock: _FakeClock) -> None:
    """`reset()` drops all per-key state => next request starts at full capacity."""
    bucket = TokenBucket(capacity=4, rate_per_sec=1.0)
    for _ in range(4):
        assert bucket.allow("ip-E")[0]
    assert not bucket.allow("ip-E")[0]
    bucket.reset()
    # Fresh bucket for the same key => full capacity again.
    granted = sum(1 for _ in range(4) if bucket.allow("ip-E")[0])
    assert granted == 4


# Suppress unused-import warning for the io alias; reserved for future
# tests that need to round-trip an actual SQLite payload.
_ = io, _SQLITE_MAGIC
