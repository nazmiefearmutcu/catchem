from __future__ import annotations

import sys
import types
from unittest.mock import patch

from catchem.reranker import RerankerModel, RerankerStub, make_reranker


def test_rerank_prefers_exact_match() -> None:
    r = RerankerStub()
    out = r.rank("Apple Inc earnings", ["AAPL", "MSFT", "Apple Inc"])
    # "Apple Inc" should outrank MSFT
    names = [n for n, _ in out]
    assert names.index("Apple Inc") < names.index("MSFT")


def test_rerank_empty_candidates() -> None:
    r = RerankerStub()
    assert r.rank("anything", []) == []


def test_make_reranker_returns_stub_when_requested() -> None:
    r = make_reranker(model_name="anything-irrelevant", use_stub=True)
    assert isinstance(r, RerankerStub)
    assert r.model_version == "stub-rerank/v1"


def test_make_reranker_falls_back_to_stub_when_model_construction_fails() -> None:
    """If the CrossEncoder import / load explodes (no GPU, no model on disk),
    the factory must NOT raise — it must transparently fall back to the
    rapidfuzz stub so the pipeline keeps running."""
    with patch("catchem.reranker.RerankerModel.__init__", side_effect=RuntimeError("no model")):
        r = make_reranker(model_name="missing/model", use_stub=False)
    # Fallback delivered a stub.
    assert isinstance(r, RerankerStub)


def test_rerank_stub_rank_is_descending_by_score() -> None:
    r = RerankerStub()
    out = r.rank("Apple", ["Apple", "Banana", "Apple Inc"])
    scores = [s for _, s in out]
    # Sorted strictly non-increasing.
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_rerank_stub_handles_empty_query() -> None:
    # Empty query — token_set_ratio still returns floats, no crash.
    r = RerankerStub()
    out = r.rank("", ["AAPL", "MSFT"])
    assert len(out) == 2
    for _, s in out:
        assert 0.0 <= s <= 1.0


def test_rerank_stub_none_query_does_not_crash() -> None:
    # `query or ""` guards a None query.
    r = RerankerStub()
    out = r.rank(None, ["AAPL"])  # type: ignore[arg-type]
    assert len(out) == 1


def test_rerank_stub_keeps_every_candidate() -> None:
    r = RerankerStub()
    out = r.rank("Apple", ["AAPL", "MSFT", "Apple Inc", "Apple"])
    assert {c for c, _ in out} == {"AAPL", "MSFT", "Apple Inc", "Apple"}


def test_rerank_stub_coerces_non_string_candidates() -> None:
    # Candidates are str()-coerced before scoring, so ints don't blow up.
    r = RerankerStub()
    out = r.rank("123", [123, 456])  # type: ignore[list-item]
    assert len(out) == 2
    assert all(0.0 <= s <= 1.0 for _, s in out)


def _install_fake_cross_encoder(predict_scores: list[float]) -> None:
    """Inject a fake `sentence_transformers` module so RerankerModel can be
    constructed and exercised with zero network / no real model download."""

    class _FakeCrossEncoder:
        def __init__(self, model_name: str, device: str = "cpu") -> None:
            self.model_name = model_name
            self.device = device

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            # Return exactly len(pairs) scores (zip strict=True contract).
            return predict_scores[: len(pairs)]

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.CrossEncoder = _FakeCrossEncoder  # type: ignore[attr-defined]
    sys.modules["sentence_transformers"] = fake_mod


def test_reranker_model_version_reports_hf_prefix() -> None:
    _install_fake_cross_encoder([])
    try:
        m = RerankerModel(model_name="cross-encoder/demo")
        assert m.model_version == "hf:cross-encoder/demo"
    finally:
        sys.modules.pop("sentence_transformers", None)


def test_reranker_model_rank_sorts_descending_by_predicted_score() -> None:
    _install_fake_cross_encoder([0.1, 0.9, 0.5])
    try:
        m = RerankerModel(model_name="cross-encoder/demo")
        out = m.rank("query", ["low", "high", "mid"])
    finally:
        sys.modules.pop("sentence_transformers", None)
    assert out == [("high", 0.9), ("mid", 0.5), ("low", 0.1)]


def test_reranker_model_rank_empty_candidates_short_circuits() -> None:
    # Must return [] WITHOUT calling predict (so [] predict_scores is fine).
    _install_fake_cross_encoder([])
    try:
        m = RerankerModel(model_name="cross-encoder/demo")
        assert m.rank("query", []) == []
    finally:
        sys.modules.pop("sentence_transformers", None)


def test_make_reranker_returns_model_when_construction_succeeds() -> None:
    _install_fake_cross_encoder([0.5])
    try:
        r = make_reranker(model_name="cross-encoder/demo", use_stub=False)
        assert isinstance(r, RerankerModel)
        assert r.model_version == "hf:cross-encoder/demo"
    finally:
        sys.modules.pop("sentence_transformers", None)
