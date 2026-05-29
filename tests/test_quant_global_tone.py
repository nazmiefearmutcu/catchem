"""Unit + endpoint coverage for the GDELT global-tone quant signal.

Two layers:

  * Pure-function layer — pins :func:`summarize_tone` (trend / state
    classification, recent-window boundary, malformed + empty points, the
    several accepted date encodings) and the :func:`compute_global_tone`
    orchestrator's per-theme aggregation + overall roll-up via a mocked
    ``fetch_tone`` (no network).
  * Endpoint layer — ``GET /api/quant/global-tone`` via TestClient with the
    network fetch monkeypatched to a fixture timeline: asserts the response
    envelope, the per-theme shape, and the degraded path.

No outbound HTTP is ever made: ``fetch_tone`` is patched at the module level
(the orchestrator calls it by its module-global name) and the endpoint test
patches the same symbol the handler imports.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.quant import global_tone as gt
from catchem.settings import load_settings, reload_settings

FIXED_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _pts(*values: float) -> list[dict]:
    """Build a chronological TimelineTone series from a list of tone values.

    Stamps each point one hour apart so the date ordering is unambiguous and
    matches GDELT's canonical ``YYYYMMDDTHHMMSSZ`` encoding.
    """
    out: list[dict] = []
    for i, v in enumerate(values):
        out.append({"date": f"20260528T{i:02d}0000Z", "value": v})
    return out


# ──────────────────────────────────────────────────────────────────────────
# summarize_tone — happy path + aggregates
# ──────────────────────────────────────────────────────────────────────────

def test_summarize_basic_aggregates() -> None:
    """latest / mean / min / max / n_points on a clean series."""
    s = gt.summarize_tone(_pts(-2.0, 0.0, 2.0, 4.0), now=FIXED_NOW)
    assert s["n_points"] == 4
    assert s["latest_tone"] == 4.0
    assert s["mean_tone"] == 1.0  # (-2+0+2+4)/4
    assert s["min_tone"] == -2.0
    assert s["max_tone"] == 4.0
    assert s["generated_at"] == FIXED_NOW.isoformat()


def test_summarize_state_improving() -> None:
    """A clearly rising series classifies as improving (trend > +threshold)."""
    s = gt.summarize_tone(_pts(-5.0, -4.0, 1.0, 3.0, 5.0), now=FIXED_NOW)
    assert s["tone_state"] == "improving"
    assert s["tone_trend"] > gt._STATE_THRESHOLD
    assert s["tone_slope"] > 0


def test_summarize_state_deteriorating() -> None:
    """A clearly falling series classifies as deteriorating (trend < -thresh)."""
    s = gt.summarize_tone(_pts(5.0, 4.0, 1.0, -3.0, -5.0), now=FIXED_NOW)
    assert s["tone_state"] == "deteriorating"
    assert s["tone_trend"] < -gt._STATE_THRESHOLD
    assert s["tone_slope"] < 0


def test_summarize_state_stable_flat() -> None:
    """A flat series is stable: zero trend, zero slope."""
    s = gt.summarize_tone(_pts(1.0, 1.0, 1.0, 1.0), now=FIXED_NOW)
    assert s["tone_state"] == "stable"
    assert s["tone_trend"] == 0.0
    assert s["tone_slope"] == 0.0


def test_summarize_state_threshold_boundary() -> None:
    """A swing smaller than the state threshold stays stable.

    Earlier window mean and recent window mean differ by less than
    ``_STATE_THRESHOLD`` (0.5), so despite a non-zero trend the state must
    NOT flip to improving/deteriorating.
    """
    # 6 points → recent window = round(6*0.4)=2 → recent=[last two], earlier=[first four].
    # earlier mean = 0.0, recent mean = 0.3 → trend 0.3 < 0.5 → stable.
    s = gt.summarize_tone(_pts(-0.2, -0.1, 0.1, 0.2, 0.3, 0.3), now=FIXED_NOW)
    assert 0.0 < s["tone_trend"] < gt._STATE_THRESHOLD
    assert s["tone_state"] == "stable"


def test_summarize_recent_window_boundary_two_points() -> None:
    """With exactly two points the recent window is one, earlier is one.

    The trend is simply ``second - first`` and the state follows. This pins
    the ``min(recent_count, n-1)`` clamp so the earlier window is never empty.
    """
    s = gt.summarize_tone(_pts(0.0, 9.0), now=FIXED_NOW)
    assert s["n_points"] == 2
    assert s["tone_trend"] == 9.0
    assert s["tone_state"] == "improving"


def test_summarize_single_point_no_trend() -> None:
    """A single point has no earlier window → trend 0, slope 0, stable."""
    s = gt.summarize_tone(_pts(7.5), now=FIXED_NOW)
    assert s["n_points"] == 1
    assert s["latest_tone"] == 7.5
    assert s["mean_tone"] == 7.5
    assert s["tone_trend"] == 0.0
    assert s["tone_slope"] == 0.0
    assert s["tone_state"] == "stable"


# ──────────────────────────────────────────────────────────────────────────
# summarize_tone — malformed / empty tolerance
# ──────────────────────────────────────────────────────────────────────────

def test_summarize_empty_series() -> None:
    """Empty input → neutral all-None summary, never raises."""
    s = gt.summarize_tone([], now=FIXED_NOW)
    assert s["n_points"] == 0
    assert s["latest_tone"] is None
    assert s["mean_tone"] is None
    assert s["min_tone"] is None
    assert s["max_tone"] is None
    assert s["tone_trend"] == 0.0
    assert s["tone_state"] == "stable"
    assert s["generated_at"] == FIXED_NOW.isoformat()


def test_summarize_drops_malformed_points() -> None:
    """Non-dict, value-less, non-numeric, NaN, inf, and bool values drop out.

    The two surviving good points (1.0, 3.0) drive the aggregates; everything
    else is silently discarded rather than raising or poisoning the mean.
    """
    timeline = [
        {"date": "20260528T000000Z", "value": 1.0},   # good
        "not-a-dict",                                   # dropped
        {"date": "20260528T010000Z"},                  # no value → dropped
        {"date": "20260528T020000Z", "value": "abc"},  # non-numeric → dropped
        {"date": "20260528T030000Z", "value": None},   # None → dropped
        {"date": "20260528T040000Z", "value": float("nan")},  # NaN → dropped
        {"date": "20260528T050000Z", "value": float("inf")},  # inf → dropped
        {"date": "20260528T060000Z", "value": True},   # bool → dropped
        {"date": "20260528T070000Z", "value": 3.0},    # good
    ]
    s = gt.summarize_tone(timeline, now=FIXED_NOW)
    assert s["n_points"] == 2
    assert s["min_tone"] == 1.0
    assert s["max_tone"] == 3.0
    assert s["mean_tone"] == 2.0


def test_summarize_numeric_string_value_coerced() -> None:
    """A numeric string value is coerced to float (defensive parsing)."""
    s = gt.summarize_tone(
        [{"date": "20260528T000000Z", "value": "2.5"}], now=FIXED_NOW
    )
    assert s["n_points"] == 1
    assert s["latest_tone"] == 2.5


def test_summarize_not_a_list() -> None:
    """A non-list timeline (defensive) yields the empty summary."""
    s = gt.summarize_tone("garbage", now=FIXED_NOW)  # type: ignore[arg-type]
    assert s["n_points"] == 0
    assert s["tone_state"] == "stable"


# ──────────────────────────────────────────────────────────────────────────
# summarize_tone — date parsing variants + ordering
# ──────────────────────────────────────────────────────────────────────────

def test_summarize_epoch_date_parsing_and_ordering() -> None:
    """Epoch-second dates parse and the series is sorted chronologically.

    Points are supplied OUT of order (later epoch first); summarize_tone must
    reorder by date so ``latest_tone`` is the chronologically newest value.
    """
    t_early = int(datetime(2026, 5, 28, 0, 0, tzinfo=UTC).timestamp())
    t_late = int(datetime(2026, 5, 28, 6, 0, tzinfo=UTC).timestamp())
    timeline = [
        {"date": t_late, "value": 8.0},    # newest, given first
        {"date": t_early, "value": -8.0},  # oldest, given second
    ]
    s = gt.summarize_tone(timeline, now=FIXED_NOW)
    assert s["n_points"] == 2
    assert s["latest_tone"] == 8.0  # newest by date, not by position
    assert s["tone_trend"] == 16.0  # 8 - (-8)
    assert s["tone_state"] == "improving"


def test_summarize_iso_date_parsing() -> None:
    """A plain ISO-8601 date with trailing Z parses (defensive path)."""
    s = gt.summarize_tone(
        [{"date": "2026-05-28T00:00:00Z", "value": 1.0}], now=FIXED_NOW
    )
    assert s["n_points"] == 1
    assert s["latest_tone"] == 1.0


def test_summarize_value_with_bad_date_still_counts() -> None:
    """A point with a good value but unparseable date still aggregates.

    Date only matters for trend ordering; a value-bearing point with a junk
    date contributes to mean/min/max/n_points (appended after dated points).
    """
    timeline = [
        {"date": "20260528T000000Z", "value": 2.0},  # dated
        {"date": "not-a-date", "value": 6.0},          # undated but valued
    ]
    s = gt.summarize_tone(timeline, now=FIXED_NOW)
    assert s["n_points"] == 2
    assert s["mean_tone"] == 4.0
    assert s["max_tone"] == 6.0


# ──────────────────────────────────────────────────────────────────────────
# fetch_tone — envelope extraction (mocked client, no network)
# ──────────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Async client stub: records the URL and returns a canned JSON payload."""

    def __init__(self, payload=None, *, raise_exc: Exception | None = None):
        self._payload = payload
        self._raise = raise_exc
        self.last_url: str | None = None

    async def get(self, url: str):
        self.last_url = url
        if self._raise is not None:
            raise self._raise
        return _FakeResponse(self._payload)


def test_fetch_tone_extracts_data_array() -> None:
    """fetch_tone pulls the first series' ``data`` out of the DOC 2.0 envelope."""
    payload = {
        "timeline": [
            {
                "series": "Average Tone",
                "data": [
                    {"date": "20260528T000000Z", "value": -1.0},
                    {"date": "20260528T010000Z", "value": 2.0},
                ],
            }
        ]
    }
    client = _FakeClient(payload)
    data = asyncio.run(gt.fetch_tone(client, "stock market", timespan="1d"))
    assert data == payload["timeline"][0]["data"]
    # The query + mode are URL-encoded into the request.
    assert client.last_url is not None
    assert "query=stock+market" in client.last_url
    assert "mode=TimelineTone" in client.last_url
    assert "timespan=1d" in client.last_url


def test_fetch_tone_transport_error_returns_empty() -> None:
    """A transport exception is swallowed → ``[]`` (fail-soft)."""
    client = _FakeClient(raise_exc=RuntimeError("connection reset"))
    assert asyncio.run(gt.fetch_tone(client, "economy")) == []


def test_fetch_tone_unexpected_shape_returns_empty() -> None:
    """A payload missing ``timeline`` → ``[]`` rather than raising."""
    client = _FakeClient({"unexpected": "shape"})
    assert asyncio.run(gt.fetch_tone(client, "crypto")) == []


# ──────────────────────────────────────────────────────────────────────────
# compute_global_tone — orchestration with mocked fetch_tone
# ──────────────────────────────────────────────────────────────────────────

def test_compute_global_tone_aggregates_by_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-theme summaries + overall roll-up from a mocked fetch.

    Each theme gets a distinct series so the by_theme map is verifiably keyed
    and the overall_tone equals the mean of the per-theme latest tones.
    """
    series_by_query = {
        "stock market": _pts(0.0, 1.0, 2.0),          # latest 2.0
        "economy OR recession": _pts(-1.0, -2.0, -3.0),  # latest -3.0
        "bitcoin OR crypto": _pts(4.0, 5.0, 6.0),     # latest 6.0
        "federal reserve OR inflation": _pts(0.0, 0.0, 1.0),  # latest 1.0
    }

    async def fake_fetch(client, query, *, timespan="1d"):
        return series_by_query.get(query, [])

    monkeypatch.setattr(gt, "fetch_tone", fake_fetch)

    result = asyncio.run(gt.compute_global_tone(gt.DEFAULT_THEMES, client=object()))

    assert set(result["by_theme"].keys()) == set(gt.DEFAULT_THEMES.keys())
    assert result["by_theme"]["markets"]["latest_tone"] == 2.0
    assert result["by_theme"]["economy"]["latest_tone"] == -3.0
    assert result["by_theme"]["crypto"]["latest_tone"] == 6.0
    assert result["by_theme"]["fed"]["latest_tone"] == 1.0
    # overall_tone = mean(2.0, -3.0, 6.0, 1.0) = 1.5
    assert result["overall_tone"] == 1.5
    assert result["overall_state"] in {"improving", "deteriorating", "stable"}
    assert isinstance(result["generated_at"], str) and result["generated_at"]


def test_compute_global_tone_all_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every fetch is empty, by_theme still lists all themes (empty) and
    overall_tone is None."""

    async def fake_fetch(client, query, *, timespan="1d"):
        return []

    monkeypatch.setattr(gt, "fetch_tone", fake_fetch)
    result = asyncio.run(gt.compute_global_tone({"markets": "stock market"}, client=object()))

    assert set(result["by_theme"].keys()) == {"markets"}
    assert result["by_theme"]["markets"]["n_points"] == 0
    assert result["overall_tone"] is None
    assert result["overall_state"] == "stable"


def test_compute_global_tone_defaults_themes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling with no themes uses DEFAULT_THEMES (markets/economy/crypto/fed)."""

    async def fake_fetch(client, query, *, timespan="1d"):
        return _pts(1.0, 2.0)

    monkeypatch.setattr(gt, "fetch_tone", fake_fetch)
    result = asyncio.run(gt.compute_global_tone(client=object()))
    assert set(result["by_theme"].keys()) == {"markets", "economy", "crypto", "fed"}


# ──────────────────────────────────────────────────────────────────────────
# GET /api/quant/global-tone — endpoint envelope + degraded path
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient over a real lifespan with background tasks disabled."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    reload_settings()
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_tone_cache() -> None:
    """Reset the endpoint's module-level TTL cache before each test.

    The 120s cache is process state; an earlier test's payload would
    otherwise be served to a later test with a different monkeypatched fetch.
    """
    from catchem import api as api_mod

    api_mod._GLOBAL_TONE_CACHE["payload"] = None
    api_mod._GLOBAL_TONE_CACHE["expires_at"] = 0.0


def test_endpoint_envelope_and_by_theme(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy-path: fixture timeline → populated by_theme + schema_version.

    The handler imports ``fetch_tone`` indirectly (compute_global_tone calls
    it by module-global name), so patching ``global_tone.fetch_tone`` covers
    the whole fan-out without any network.
    """

    async def fake_fetch(http_client, query, *, timespan="1d"):
        return _pts(-1.0, 0.0, 1.0, 2.0)  # latest 2.0, rising

    monkeypatch.setattr(gt, "fetch_tone", fake_fetch)

    r = client.get("/api/quant/global-tone")
    assert r.status_code == 200, r.text
    data = r.json()

    for key in (
        "schema_version",
        "degraded",
        "generated_at",
        "by_theme",
        "overall_tone",
        "overall_state",
    ):
        assert key in data, f"missing {key}"

    assert data["schema_version"] == 1
    assert data["degraded"] is False
    assert set(data["by_theme"].keys()) == {"markets", "economy", "crypto", "fed"}

    # Each theme summary carries the documented signal fields.
    for theme in data["by_theme"].values():
        for k in ("latest_tone", "mean_tone", "tone_trend", "tone_state", "n_points"):
            assert k in theme, theme
        assert theme["n_points"] == 4
        assert theme["latest_tone"] == 2.0

    # overall_tone = mean of identical 2.0 latests = 2.0.
    assert data["overall_tone"] == 2.0
    assert data["overall_state"] in {"improving", "deteriorating", "stable"}


def test_endpoint_degraded_when_all_fetches_fail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-empty upstream → 200 with degraded=true and empty per-theme summaries."""

    async def empty_fetch(http_client, query, *, timespan="1d"):
        return []

    monkeypatch.setattr(gt, "fetch_tone", empty_fetch)

    r = client.get("/api/quant/global-tone")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["degraded"] is True
    assert data["overall_tone"] is None
    assert data["overall_state"] == "stable"
    # Themes still listed, each with a neutral empty summary.
    assert set(data["by_theme"].keys()) == {"markets", "economy", "crypto", "fed"}
    for theme in data["by_theme"].values():
        assert theme["n_points"] == 0
        assert theme["latest_tone"] is None


def test_endpoint_degraded_when_compute_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the orchestrator itself raises, the endpoint still 200s, degraded.

    Belt-and-suspenders: compute_global_tone is fail-soft, but the handler
    wraps it so even an unexpected explosion can't 500 the cockpit.
    """

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("catchem.quant.global_tone.compute_global_tone", boom)

    r = client.get("/api/quant/global-tone")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["degraded"] is True
    assert data["by_theme"] == {}
    assert data["overall_tone"] is None


# ──────────────────────────────────────────────────────────────────────────────
# summarize_tone — date-less points must not masquerade as "latest" (v79 fix)
#
# `_order_points` used to append date-less points to the END of the value
# sequence, so `latest_tone = values[-1]` could return a date-less straggler and
# the trend's recent window could be anchored on one. Order-sensitive stats now
# run over DATED points sorted chronologically; order-free aggregates still see
# every valid point.
# ──────────────────────────────────────────────────────────────────────────────


def test_summarize_tone_latest_is_newest_dated_not_input_order() -> None:
    # Points arrive OUT OF ORDER and one is date-less. latest_tone must be the
    # most-recent DATED point — never the date-less straggler that happens to be
    # last in the input list.
    timeline = [
        {"date": "20260101T000000Z", "value": -3.0},  # oldest
        {"date": "20260103T000000Z", "value": 5.0},   # newest DATED
        {"date": "20260102T000000Z", "value": 1.0},   # middle
        {"value": 99.0},                               # date-less, last in input
    ]
    s = gt.summarize_tone(timeline)
    assert s["latest_tone"] == 5.0, "latest = newest dated point, not the date-less last item"
    # All valid points still count toward the order-free aggregates.
    assert s["n_points"] == 4
    assert s["max_tone"] == 99.0
    assert s["min_tone"] == -3.0


def test_summarize_tone_dateless_point_excluded_from_trend_and_latest() -> None:
    # A date-less extreme must not anchor the trend window or the latest. A clean
    # improving dated series plus an injected date-less spike yields IDENTICAL
    # latest/trend/slope (the spike is excluded from the ordered sequence) while
    # still moving the order-free min.
    dated_improving = [
        {"date": f"202601{d:02d}T000000Z", "value": float(d)} for d in range(1, 11)
    ]
    base = gt.summarize_tone(dated_improving)
    with_spike = gt.summarize_tone([*dated_improving, {"value": -500.0}])

    assert base["tone_state"] == "improving"  # sanity: the dated series trends up
    assert with_spike["latest_tone"] == base["latest_tone"]
    assert with_spike["tone_trend"] == base["tone_trend"]
    assert with_spike["tone_slope"] == base["tone_slope"]
    # …but the spike does move the order-free range.
    assert with_spike["min_tone"] == -500.0


def test_summarize_tone_all_dateless_falls_back_to_input_order() -> None:
    # When NO point carries a usable date we fall back to GDELT's as-sent order
    # (oldest→newest), so a fully date-less series still yields a latest/trend.
    s = gt.summarize_tone([{"value": 1.0}, {"value": 2.0}, {"value": 9.0}])
    assert s["latest_tone"] == 9.0  # last in input order
    assert s["n_points"] == 3


# ──────────────────────────────────────────────────────────────────────────────
# /api/quant/global-tone single-flight (v80 audit fix)
#
# The old design held a threading.Lock only around the cache read/write, NOT
# across the awaited GDELT fan-out, so N concurrent misses each fanned out —
# the very stampede the cache exists to prevent. The fix holds a per-loop
# asyncio.Lock across the fetch (double-checked inside), coalescing concurrent
# misses into ONE compute.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_single_flight_coalesces_concurrent_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    reload_settings()
    app = create_app(load_settings())

    calls = {"n": 0}

    async def _slow_compute(themes=None, *, client=None):
        # Count fan-outs; sleep so the concurrent requests overlap inside the
        # single-flight window (the first holds the lock, the rest wait).
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "by_theme": {"markets": {"n_points": 3, "latest_tone": 1.0}},
            "overall_tone": 1.0,
            "overall_state": "stable",
        }

    monkeypatch.setattr("catchem.quant.global_tone.compute_global_tone", _slow_compute)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        results = await asyncio.gather(
            *(ac.get("/api/quant/global-tone") for _ in range(6))
        )

    assert all(r.status_code == 200 for r in results)
    # All six requests return the same payload…
    assert all(r.json()["overall_tone"] == 1.0 for r in results)
    # …but only ONE actually fanned out to GDELT (single-flight coalescing).
    assert calls["n"] == 1, "6 concurrent misses must coalesce into a single compute"


@pytest.mark.asyncio
async def test_global_tone_lock_is_per_running_loop() -> None:
    # The single-flight lock is created lazily per running loop and is stable
    # within a loop (so concurrent requests on that loop share it).
    from catchem import api as api_mod

    lock_a = api_mod._global_tone_lock()
    lock_b = api_mod._global_tone_lock()
    assert lock_a is lock_b, "same running loop must reuse one lock"
    assert isinstance(lock_a, asyncio.Lock)
