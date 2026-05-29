from __future__ import annotations

import numpy as np

from catchem.embeddings import EmbedderStub, VectorIndex, cosine


def test_stub_vector_is_normalized_and_stable() -> None:
    e = EmbedderStub()
    v1 = e.encode("Fed raises rates")
    v2 = e.encode("Fed raises rates")
    assert np.allclose(v1, v2)
    assert 0.9 <= np.linalg.norm(v1) <= 1.1


def test_similar_texts_have_higher_cosine() -> None:
    e = EmbedderStub()
    a = e.encode("Apple beats earnings expectations Q4 2025")
    a2 = e.encode("Apple beats earnings expectations Q4 2025 raises guidance")
    b = e.encode("Local football team wins championship")
    assert cosine(a, a2) > cosine(a, b)


def test_punctuation_attached_tokens_hash_the_same_as_clean(
) -> None:
    """BUG-U regression: pre-fix `text.lower().split()` kept punctuation on
    tokens — `"fed,"` and `"fed"` hashed to DIFFERENT buckets, so the same
    word in a press-wire title and a sentence body produced misaligned
    embedding dimensions. With proper alphanumeric tokenisation the two
    texts must produce the same vector when the only difference is
    punctuation."""
    e = EmbedderStub()
    clean = e.encode("fed raises rates")
    punct = e.encode("fed, raises rates.")
    # Identical token set → identical embedding.
    assert np.allclose(clean, punct), (
        "Punctuation-attached tokens must hash the same as clean tokens. "
        f"cosine(clean, punct)={cosine(clean, punct):.4f}"
    )
    # And the cosine must be ~1.0 to make the property test visible.
    assert cosine(clean, punct) > 0.999


def test_vector_index_round_trip(tmp_path) -> None:
    idx = VectorIndex(tmp_path / "v")
    e = EmbedderStub()
    vec = e.encode("hello")
    idx.save("cap-1", vec)
    loaded = idx.load("cap-1")
    assert loaded is not None
    assert np.allclose(loaded, vec)


def test_vector_index_cache_avoids_disk_read_on_repeat_load(tmp_path) -> None:
    """BUG-DD: pre-fix every `load`/`nearest` re-read every .npy file from
    disk. With the in-memory cache, the second load of the same id is
    served from RAM (verified by patching np.load and counting calls).
    """
    from unittest.mock import patch

    import catchem.embeddings as emb

    idx = emb.VectorIndex(tmp_path / "v")
    e = emb.EmbedderStub()
    vec = e.encode("hello")
    idx.save("cap-1", vec)
    # First load — should come from in-memory cache because save() warmed it.
    with patch.object(emb.np, "load") as np_load_spy:
        loaded = idx.load("cap-1")
        assert loaded is not None
        assert np_load_spy.call_count == 0, (
            "save() should warm the cache so the immediate load() bypasses disk"
        )


def test_vector_index_nearest_only_reloads_uncached_files(tmp_path) -> None:
    """`nearest` should only `np.load` files NOT already in the cache.
    The cache is warmed by `save`, so a session that wrote N records and
    then ran nearest() must do ZERO disk reads.
    """
    from unittest.mock import patch

    import catchem.embeddings as emb

    idx = emb.VectorIndex(tmp_path / "v")
    e = emb.EmbedderStub()
    for i in range(3):
        idx.save(f"c{i}", e.encode(f"text {i}"))
    q = e.encode("query")
    with patch.object(emb.np, "load") as np_load_spy:
        results = idx.nearest(q, k=3)
        assert len(results) == 3
        assert np_load_spy.call_count == 0, (
            "nearest() over fully-warm cache must not hit disk"
        )


def test_nearest_returns_ranked_candidates(tmp_path) -> None:
    idx = VectorIndex(tmp_path / "v")
    e = EmbedderStub()
    idx.save("c1", e.encode("Fed raises rates"))
    idx.save("c2", e.encode("Apple earnings"))
    idx.save("c3", e.encode("Fed hikes interest rates again"))
    q = e.encode("Federal Reserve raises interest rates")
    ranked = idx.nearest(q, k=3)
    # c1 / c3 should be on top
    assert ranked[0][0] in ("c1", "c3")


def test_vector_index_concurrent_save_and_nearest_is_thread_safe(tmp_path) -> None:
    # Regression (v80 audit HIGH): `save()` runs in the news-poller's 4 ingest
    # worker threads while `nearest()` may sweep the cache from another thread.
    # With a plain dict this raced — "RuntimeError: dictionary changed size
    # during iteration" — or silently dropped writes. The lock + snapshot must
    # make concurrent save()/nearest() crash-free.
    import threading

    idx = VectorIndex(tmp_path / "v")
    e = EmbedderStub()
    for i in range(10):  # seed so nearest() has something to iterate immediately
        idx.save(f"seed-{i}", e.encode(f"seed text {i}"))

    errors: list[BaseException] = []

    def _writer(base: str) -> None:
        try:
            for i in range(120):
                idx.save(f"{base}-{i}", e.encode(f"{base} story {i}"))
        except BaseException as exc:  # capture ANY error raised on the thread
            errors.append(exc)

    def _reader() -> None:
        try:
            q = e.encode("query text")
            for _ in range(80):
                idx.nearest(q, k=5)
        except BaseException as exc:  # capture ANY error raised on the thread
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(f"w{n}",)) for n in range(3)]
    threads += [threading.Thread(target=_reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent save()/nearest() raised: {errors[:3]}"
    assert idx.load("w0-0") is not None, "writes must still land under contention"


def test_vector_index_cache_is_lru_capped(tmp_path, monkeypatch) -> None:
    # The in-memory hot layer must not grow without bound — past the cap the
    # oldest entry is evicted; the durable .npy remains so load() re-reads it.
    import catchem.embeddings as emb

    monkeypatch.setattr(emb.VectorIndex, "_CACHE_CAP", 5)
    idx = emb.VectorIndex(tmp_path / "v")
    e = EmbedderStub()
    for i in range(12):
        idx.save(f"c{i}", e.encode(f"text {i}"))

    assert len(idx._cache) <= 5, "cache must stay within the LRU cap"
    assert idx.load("c11") is not None, "most-recent write resident"
    assert idx.load("c0") is not None, "evicted entry re-loads from durable .npy"
