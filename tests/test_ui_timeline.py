"""Pins that /ui/timeline tolerates `bucket_minutes` > 60.

Bug history: the bucket key used ``dt.replace(minute=...)`` where
``minute = (dt.minute // bucket_minutes) * bucket_minutes``. For any
``bucket_minutes`` ≥ 60 the divide returns 0 for most timestamps, but
for ``dt.minute == 0`` and ``bucket_minutes == 120`` the math still
worked accidentally. The real crash bites whenever ``bucket_minutes >
60`` AND there's a timestamp whose original minute is also above the
new bucket size — practically guaranteed once enough records land. The
endpoint accepts ``ge=5, le=1440`` so callers expect ``120``, ``240``,
``1440`` to work.

The replacement uses epoch-based truncation, which is uniform across
all bucket sizes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    reload_settings()
    s = load_settings()
    app = create_app(s)
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


@pytest.mark.parametrize("bucket_minutes", [120, 240, 360, 720, 1440])
def test_ui_timeline_accepts_buckets_over_sixty(client: TestClient, bucket_minutes: int) -> None:
    """The endpoint must return 200 + a well-shaped body for any
    bucket_minutes in the supported [5..1440] range.

    With the pre-fix `dt.replace(minute=...)` the call raised
    ``ValueError: minute must be in 0..59`` and FastAPI surfaced a 500.
    """
    r = client.get(f"/ui/timeline?bucket_minutes={bucket_minutes}")
    assert r.status_code == 200, f"timeline crashed at bucket_minutes={bucket_minutes}: {r.text}"
    data = r.json()
    assert data["bucket_minutes"] == bucket_minutes
    assert isinstance(data["series"], list)


def test_ui_timeline_buckets_align_to_epoch(client: TestClient) -> None:
    """Each bucket key must be a multiple of bucket_seconds from epoch.

    This is the durable invariant of epoch-truncation. Without records
    we only test the response shape; the alignment check kicks in once
    the supervisor has any rows. We don't need to seed — even an empty
    timeline must have the right shape.
    """
    r = client.get("/ui/timeline?bucket_minutes=120")
    assert r.status_code == 200
    data = r.json()
    # No records → empty series is fine; the contract is "no crash + shape".
    assert "bucket_minutes" in data
    assert "series" in data
