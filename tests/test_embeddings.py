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
