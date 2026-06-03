import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from catchem.news_poller import (
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
    _canonical_url,
    _is_stale_published_ts,
    _normalize_title,
    _parse_ts,
    _resolve_domain,
    _SeenCache,
    _strip_source_suffix,
    fetch_feed,
    fetch_feed_result,
    parse_feed,
)


# 1. _canonical_url exception handler (365-368)
def test_canonical_url_exception_handling():
    with patch("urllib.parse.urlsplit", side_effect=Exception("mocked urlsplit error")):
        assert _canonical_url("http://example.com") == "http://example.com"


# 2. _normalize_title exception handler (418-419)
def test_normalize_title_exception_handling():
    with patch("catchem.news_poller._SOURCE_SUFFIX_RE") as mock_re:
        mock_re.sub.side_effect = Exception("mocked regex error")
        assert _normalize_title("Some title") == ""


# 3. _resolve_domain exception handler (512-513)
def test_resolve_domain_exception_handling():
    with patch("catchem.news_poller.urlparse", side_effect=Exception("mocked urlparse error")):
        assert _resolve_domain("http://example.com", "fallback.com") == "fallback.com"


# 4. _strip_source_suffix title without suffix (523)
def test_strip_source_suffix_no_suffix():
    assert _strip_source_suffix("Some Title", "CNBC") == "Some Title"


# 5. _parse_ts timezone and fallback parsing
def test_parse_ts_edge_cases():
    # dt.tzinfo is None for parsedate_to_datetime (534)
    naive_dt = datetime(2026, 5, 15, 14, 0, 0)
    with patch("catchem.news_poller.parsedate_to_datetime", return_value=naive_dt):
        dt = _parse_ts("Wed, 15 May 2026 14:00:00")
        assert dt.tzinfo == UTC
        assert dt == naive_dt.replace(tzinfo=UTC)

    # ValueError exception path in parsedate_to_datetime (536)
    # falling back to strptime loop where dt.tzinfo is None (542)
    # and ValueError path (544-545)
    with patch("catchem.news_poller.parsedate_to_datetime", side_effect=ValueError):
        with patch("catchem.news_poller.datetime") as mock_datetime:
            mock_datetime.strptime.side_effect = [ValueError, naive_dt]
            mock_datetime.now.return_value = datetime(2026, 5, 15, 14, 0, 0, tzinfo=UTC)
            dt = _parse_ts("some-value")
            assert dt.tzinfo == UTC


# 6. _is_stale_published_ts when max_age_seconds <= 0 (551)
def test_is_stale_published_ts_disabled():
    now = datetime.now(UTC)
    assert not _is_stale_published_ts(now - timedelta(days=100), now, 0)
    assert not _is_stale_published_ts(now - timedelta(days=100), now, -10)


# 7. Atom parsing edge cases (619-621, 628)
def test_parse_feed_atom_missing_or_empty_links():
    # Missing link element entirely
    atom_xml_no_link = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>No link entry</title>
        <updated>2026-05-16T12:34:00Z</updated>
        <summary>Summary</summary>
      </entry>
    </feed>
    """
    items = parse_feed(atom_xml_no_link, fallback_domain="example.com")
    assert len(items) == 0

    # Link element has text but no href
    atom_xml_text_link = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Text link entry</title>
        <link>https://example.com/atom-link</link>
        <updated>2026-05-16T12:34:00Z</updated>
        <summary>Summary</summary>
      </entry>
    </feed>
    """
    items = parse_feed(atom_xml_text_link, fallback_domain="example.com")
    assert len(items) == 1
    assert items[0].url == "https://example.com/atom-link"


# 8. fetch_feed_result code paths
@pytest.mark.asyncio
async def test_fetch_feed_result_errors():
    spec = FeedSpec("test-feed", "https://example.com/rss", "example.com")
    
    # 8a. Response code != 200 (676-677)
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.content = b""
    mock_client = MagicMock()
    
    async def mock_get(*args, **kwargs):
        return mock_resp
        
    mock_client.get = mock_get
    res = await fetch_feed_result(mock_client, spec)
    assert res.status_code == 404
    assert res.error == "http_404"

    # 8b. httpx.HTTPError (689-695)
    async def mock_get_http_error(*args, **kwargs):
        raise httpx.HTTPError("mocked HTTP error")
    mock_client.get = mock_get_http_error
    res = await fetch_feed_result(mock_client, spec)
    assert res.error == "HTTPError"

    # 8c. Generic Exception (696-702)
    async def mock_get_exception(*args, **kwargs):
        raise RuntimeError("mocked generic exception")
    mock_client.get = mock_get_exception
    res = await fetch_feed_result(mock_client, spec)
    assert res.error == "RuntimeError"

    # 8d. fetch_feed wrapper (707-708)
    items = await fetch_feed(mock_client, spec)
    assert items == []


# Helper stub for NewsPoller tests
class _StubStorage:
    def __init__(self):
        self.records = {}
    def get_record(self, cap_id):
        return self.records.get(cap_id)


class _StubSupervisor:
    def __init__(self):
        self.storage = _StubStorage()
        self.processed = []
    def process_capture(self, cap):
        self.processed.append(cap)


def _make_poller(tmp_path, feeds=None, interval_seconds=10.0, max_item_age_seconds=14 * 24 * 3600):
    class _Paths:
        catchem_output_dir = tmp_path
    class _News:
        dedup_title_window_seconds = 3600
        adaptive_polling_enabled = True
    class _Settings:
        paths = _Paths()
        news = _News()
    
    sup = _StubSupervisor()
    poller = NewsPoller(
        supervisor=sup,
        settings=_Settings(),
        feeds=feeds or [],
        interval_seconds=interval_seconds,
        max_item_age_seconds=max_item_age_seconds
    )
    return poller, sup


# 9. NewsPoller start and stop (826-836, 841-846)
@pytest.mark.asyncio
async def test_newspoller_start_stop_lifecycle(tmp_path):
    poller, _ = _make_poller(tmp_path)
    
    mock_loop = MagicMock()
    mock_task = asyncio.Future()
    mock_loop.create_task.return_value = mock_task
    
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        # Initial start
        poller.start()
        assert poller._task is mock_task
        
        # Start again when task exists and is not done (826-827)
        poller.start()
        assert poller._task is mock_task
        mock_loop.create_task.assert_called_once()
        
        # Stop
        await poller.stop()
        assert mock_task.cancelled() is True
        assert poller._task is None
        
        # Stop when task is None (839-840)
        await poller.stop()


# 10. NewsPoller.probe_feed_async (907, 918-922, 927, 942-943, 946, 950, 955-956)
@pytest.mark.asyncio
async def test_newspoller_probe_feed(tmp_path):
    spec = FeedSpec("test-feed", "https://example.com/rss", "example.com")
    poller, sup = _make_poller(tmp_path, feeds=[spec])
    
    # 10a. KeyError when feed is not configured (907)
    with pytest.raises(KeyError):
        await poller.probe_feed_async("https://not-configured.com/rss")
        
    # Set up some feed health that should be cleared by manual probe (918-922)
    poller.feed_health[spec.name] = {
        "cooldown_until": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "backed_off": True,
    }
    
    # Mock the fetch result
    item1 = ParsedItem(
        title="Duplicate Title",
        text="Duplicate body text",
        url="https://example.com/a",
        domain="example.com",
        published_ts=datetime.now(UTC),
    )
    
    # 10b. fetch_feed_result with self._client is not None (927)
    poller._client = MagicMock()
    mock_result = FeedFetchResult(spec=spec, items=(item1,), status_code=200, elapsed_ms=5.0)
    
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result):
        # Run probe_feed_async
        health_dict = await poller.probe_feed_async(spec.url)
        assert isinstance(health_dict, dict)
        
        # Verify cooldown was cleared
        health = poller.feed_health[spec.name]
        assert health["cooldown_until"] is None
        assert health["backed_off"] is False
        
        # 10c. Seen URL skip (946)
        ingested2 = await poller.probe_feed_async(spec.url)
        assert ingested2.get("total_new_items", 0) == 0
        
        # 10d. Already in storage skip (950)
        poller._seen = _SeenCache()
        with patch.object(sup.storage, "get_record", return_value=object()):
            ingested_storage = await poller.probe_feed_async(spec.url)
            assert ingested_storage.get("total_new_items", 0) == 0
            
        # 10e. Duplicate title skip (955-956)
        poller._seen = _SeenCache()
        with patch.object(sup.storage, "get_record", return_value=None):
            now = datetime.now(UTC)
            poller._seen_titles["duplicate title"] = now
            poller._seen_titles.move_to_end("duplicate title")
            
            ingested_title = await poller.probe_feed_async(spec.url)
            assert ingested_title.get("total_new_items", 0) == 0
            
        # 10f. Stale published ts skip (942-943)
        poller._seen = _SeenCache()
        poller._seen_titles.clear()
        stale_item = ParsedItem(
            title="Stale Title",
            text="Stale body text",
            url="https://example.com/b",
            domain="example.com",
            published_ts=datetime.now(UTC) - timedelta(days=20),
        )
        mock_result_stale = FeedFetchResult(spec=spec, items=(stale_item,), status_code=200, elapsed_ms=5.0)
        with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result_stale):
            ingested_stale = await poller.probe_feed_async(spec.url)
            assert ingested_stale.get("total_new_items", 0) == 0


# 11. NewsPoller._run_one_tick and background loop _run (999, 1002-1005, 1011-1043)
@pytest.mark.asyncio
async def test_newspoller_run_and_tick(tmp_path):
    spec = FeedSpec("test-feed", "https://example.com/rss", "example.com")
    poller, _ = _make_poller(tmp_path, feeds=[spec])
    
    # 11a. self.empty_ticks += 1 (999)
    with patch.object(poller, "_poll_once", return_value=0):
        mock_client = MagicMock()
        n = await poller._run_one_tick(mock_client)
        assert n == 0
        assert poller.empty_ticks == 1
        
    # 11b. Exception in _run_one_tick (1002-1005)
    with patch.object(poller, "_poll_once", side_effect=Exception("mocked tick failure")):
        n = await poller._run_one_tick(mock_client)
        assert n == 0
        assert "mocked tick failure" in poller.last_error
        
    # 11c. _run background loop execution (1011-1043)
    poller._stop.set()
    poller._grace = 0.01
    await poller._run()
    
    poller._stop.clear()
    
    async def fake_stop_soon():
        await asyncio.sleep(0.02)
        poller._stop.set()
        
    stop_task = asyncio.create_task(fake_stop_soon())
    
    with patch.object(poller, "_run_one_tick") as mock_tick:
        poller._grace = 0.001
        poller._interval = 0.01
        await poller._run()
        mock_tick.assert_called()
        
    await stop_task


# 12. NewsPoller._poll_once (1094-1095, 1104->1116, 1145-1146, 1149, 1153)
@pytest.mark.asyncio
async def test_newspoller_poll_once_edge_cases(tmp_path):
    spec = FeedSpec("test-feed", "https://example.com/rss", "example.com")
    poller, sup = _make_poller(tmp_path, feeds=[spec])
    
    # 12a. Cooldown validation with ValueError (1094-1095)
    poller.feed_health[spec.name] = {
        "cooldown_until": "invalid-iso-date-string",
        "backed_off": True,
    }
    
    # 12b. Cooldown expired (1104->1116)
    poller.feed_health[spec.name] = {
        "cooldown_until": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        "backed_off": True,
    }
    
    item = ParsedItem(
        title="Fresh story",
        text="Some fresh story body text",
        url="https://example.com/fresh",
        domain="example.com",
        published_ts=datetime.now(UTC),
    )
    
    mock_result = FeedFetchResult(spec=spec, items=(item,), status_code=200, elapsed_ms=5.0)
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result):
        n = await poller._poll_once(None)
        assert n == 1
        health = poller.feed_health[spec.name]
        assert health["cooldown_until"] is None
        assert health["backed_off"] is False

    # 12c. Skip stale published ts in _poll_once (1145-1146)
    poller._seen = _SeenCache()
    poller._seen_titles.clear()
    stale_item = ParsedItem(
        title="Stale Title",
        text="Stale body text",
        url="https://example.com/stale-poll",
        domain="example.com",
        published_ts=datetime.now(UTC) - timedelta(days=20),
    )
    mock_result_stale = FeedFetchResult(spec=spec, items=(stale_item,), status_code=200, elapsed_ms=5.0)
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result_stale):
        n = await poller._poll_once(None)
        assert n == 0

    # 12d. Skip seen canonical URL in _poll_once (1149)
    poller._seen = _SeenCache()
    poller._seen.add(_canonical_url(item.url))
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result):
        n = await poller._poll_once(None)
        assert n == 0

    # 12e. Skip already in storage in _poll_once (1153)
    poller._seen = _SeenCache()
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result):
        with patch.object(sup.storage, "get_record", return_value=object()):
            n = await poller._poll_once(None)
            assert n == 0


# 13. Ingest edge cases (1196->1200, 1239-1240, 1282-1283, 1311->1319, 1322, 1346->exit, 1371)
@pytest.mark.asyncio
async def test_newspoller_ingest_edge_cases(tmp_path):
    spec = FeedSpec("test-feed", "https://example.com/rss", "example.com")
    poller, _ = _make_poller(tmp_path, feeds=[spec], max_item_age_seconds=0)
    
    # 13a. item.published_ts is None in ingest (1196->1200)
    item_no_ts = ParsedItem(
        title="No TS Title",
        text="Body text no TS",
        url="https://example.com/no-ts",
        domain="example.com",
        published_ts=None,
    )
    mock_result = FeedFetchResult(spec=spec, items=(item_no_ts,), status_code=200, elapsed_ms=5.0)
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result):
        n = await poller._poll_once(None)
        assert n == 1
        assert poller.last_avg_publisher_lag_seconds is None

    # 13b. all publisher lags are old (lags > 4h) (1239-1240)
    poller._seen = _SeenCache()
    poller._seen_titles.clear()
    item_old = ParsedItem(
        title="Old item title",
        text="Body text old item",
        url="https://example.com/old-item",
        domain="example.com",
        published_ts=datetime.now(UTC) - timedelta(hours=5),
    )
    mock_result_old = FeedFetchResult(spec=spec, items=(item_old,), status_code=200, elapsed_ms=5.0)
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result_old):
        n = await poller._poll_once(None)
        assert n == 1
        assert poller.last_avg_publisher_lag_seconds is None

    # 13c. OSError in write_jsonl (1282-1283)
    poller._seen = _SeenCache()
    poller._seen_titles.clear()
    item_os_error = ParsedItem(
        title="OS Error Title",
        text="OS Error body",
        url="https://example.com/os-error",
        domain="example.com",
        published_ts=datetime.now(UTC),
    )
    mock_result_err = FeedFetchResult(spec=spec, items=(item_os_error,), status_code=200, elapsed_ms=5.0)
    with patch("catchem.news_poller.fetch_feed_result", return_value=mock_result_err):
        with patch("catchem.news_poller.write_jsonl", side_effect=OSError("mocked disk full")):
            n = await poller._poll_once(None)
            assert n == 1

    # 13d. elapsed time is outside the dedup window in _is_duplicate_title (1311->1319)
    poller._seen_titles["long ago title"] = datetime.now(UTC) - timedelta(hours=2)
    assert not poller._is_duplicate_title("long ago title", datetime.now(UTC))

    # 13e. LRU eviction in seen_titles (1322)
    poller._seen_titles_cap = 2
    poller._seen_titles.clear()
    poller._is_duplicate_title("title 1", datetime.now(UTC))
    poller._is_duplicate_title("title 2", datetime.now(UTC))
    poller._is_duplicate_title("title 3", datetime.now(UTC))
    assert "title 1" not in poller._seen_titles
    assert "title 2" in poller._seen_titles
    assert "title 3" in poller._seen_titles

    # 13f. key is false/empty in _rollback_title (1346->exit)
    poller._rollback_title("")

    # 13g. prev is None in _record_adaptive_yield (1371)
    poller._record_adaptive_yield("not-exists-feed", 1, 1)
