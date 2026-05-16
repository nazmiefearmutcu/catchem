from __future__ import annotations

import numpy as np

from fusion_stack.embeddings import EmbedderStub, VectorIndex, cosine


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


def test_vector_index_round_trip(tmp_path) -> None:
    idx = VectorIndex(tmp_path / "v")
    e = EmbedderStub()
    vec = e.encode("hello")
    idx.save("cap-1", vec)
    loaded = idx.load("cap-1")
    assert loaded is not None
    assert np.allclose(loaded, vec)


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
