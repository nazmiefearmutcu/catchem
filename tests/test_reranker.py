from __future__ import annotations

from catchem.reranker import RerankerStub


def test_rerank_prefers_exact_match() -> None:
    r = RerankerStub()
    out = r.rank("Apple Inc earnings", ["AAPL", "MSFT", "Apple Inc"])
    # "Apple Inc" should outrank MSFT
    names = [n for n, _ in out]
    assert names.index("Apple Inc") < names.index("MSFT")


def test_rerank_empty_candidates() -> None:
    r = RerankerStub()
    assert r.rank("anything", []) == []
