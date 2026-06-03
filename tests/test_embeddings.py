from __future__ import annotations

import os

import numpy as np
import pytest

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


def test_stub_empty_or_no_tokens() -> None:
    e = EmbedderStub()
    assert np.allclose(e.encode(""), np.zeros(64))
    assert np.allclose(e.encode(",,,"), np.zeros(64))


def test_stub_encode_many() -> None:
    e = EmbedderStub()
    res = e.encode_many(["hello", "world"])
    assert res.shape == (2, 64)
    res_empty = e.encode_many([])
    assert res_empty.shape == (0, 64)


def test_cosine_mismatch() -> None:
    a = np.zeros(64)
    b = np.zeros(384)
    assert cosine(a, b) == 0.0


def test_vector_index_delete_and_missing_load(tmp_path) -> None:
    idx = VectorIndex(tmp_path / "v")
    idx.save("c1", np.zeros(64))
    assert idx.load("c1") is not None
    idx.delete("c1")
    assert idx.load("c1") is None
    # loading a totally random missing one
    assert idx.load("c_missing") is None


def test_vector_index_save_exception_cleanup(tmp_path, monkeypatch) -> None:
    idx = VectorIndex(tmp_path / "v")
    def mock_replace(src, dst):
        raise OSError("Mock replace error")
    monkeypatch.setattr(os, "replace", mock_replace)
    
    with pytest.raises(OSError, match="Mock replace error"):
        idx.save("c1", np.zeros(64))
    
    # Ensure no leftover temp files remain in directory
    temp_files = list((tmp_path / "v").glob("*.npytmp"))
    assert len(temp_files) == 0


def test_make_embedder_modes() -> None:
    from catchem.embeddings import make_embedder
    # 1. use_stub is True
    stub = make_embedder("some_model", use_stub=True)
    assert isinstance(stub, EmbedderStub)
    
    # 2. use_stub is False, but SentenceTransformer is not mockable/fails
    # (should fall back to EmbedderStub)
    stub_fallback = make_embedder("nonexistent_model_name", use_stub=False)
    assert isinstance(stub_fallback, EmbedderStub)


def test_embedder_model_happy_path(monkeypatch) -> None:
    # Mock sentence_transformers module
    import sys
    from unittest.mock import MagicMock
    
    mock_model = MagicMock()
    mock_model.get_sentence_embedding_dimension.return_value = 384
    mock_model.encode.side_effect = lambda texts, **kwargs: (
        np.zeros((len(texts), 384)) if isinstance(texts, list) else np.zeros(384)
    )
    
    mock_st_class = MagicMock(return_value=mock_model)
    
    # Inject our mock into sys.modules
    mock_st_module = MagicMock()
    mock_st_module.SentenceTransformer = mock_st_class
    monkeypatch.setitem(sys.modules, "sentence_transformers", mock_st_module)
    
    # Now test EmbedderModel
    from catchem.embeddings import EmbedderModel, make_embedder
    model = EmbedderModel("mock-model")
    assert model.model_version == "hf:mock-model"
    
    # Test encode single
    v = model.encode("hello")
    assert v.shape == (384,)
    
    # Test encode_many
    res = model.encode_many(["hello", "world"])
    assert res.shape == (2, 384)
    
    # Test encode_many empty
    res_empty = model.encode_many([])
    assert res_empty.shape == (0, 384)
    
    # Test make_embedder with mock working
    model_instance = make_embedder("mock-model", use_stub=False)
    assert isinstance(model_instance, EmbedderModel)


def test_vector_index_nearest_vanished_file(tmp_path, monkeypatch) -> None:
    idx = VectorIndex(tmp_path / "v")
    e = EmbedderStub()
    # Save a file, so it exists in glob
    idx.save("c1", e.encode("hello"))
    # Evict it from cache so nearest() tries to reload it
    with idx._cache_lock:
        idx._cache.pop("c1", None)
    
    # Mock np.load to raise FileNotFoundError when reading c1
    original_load = np.load
    def mock_np_load(path, *args, **kwargs):
        if "c1.npy" in str(path):
            raise FileNotFoundError("Mock vanished file")
        return original_load(path, *args, **kwargs)
    
    monkeypatch.setattr(np, "load", mock_np_load)
    
    # nearest should not fail but skip it
    results = idx.nearest(e.encode("query"), k=5)
    assert len(results) == 0


