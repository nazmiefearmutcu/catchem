"""Tests for process pool isolation and caching behavior in CatchemService."""

from __future__ import annotations

import os
import resource
from collections.abc import Callable

import numpy as np
import pytest

from catchem.schemas import AwarenessCaptureView
from catchem.service import (
    FIFOCache,
    _init_worker,
    _worker_classify_sentiment,
    _worker_classify_zero_shot,
    _worker_encode_embedding,
    _worker_encode_many_embedding,
    _worker_rank_reranker,
    build_service,
)
from catchem.settings import (
    CatchemMode,
    Settings,
)
from catchem.taxonomy import default_taxonomy_path, load_taxonomy


def test_service_initializes_with_process_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_PROCESSES", "1")
    settings = Settings(mode=CatchemMode.PRODUCTION_SAFE)
    svc = build_service(settings)
    try:
        assert svc.use_isolation is True
        assert svc._pool is not None
        assert svc.zero_shot.__class__.__name__ == "ProcessIsolatedZeroShot"
        assert svc.sentiment.__class__.__name__ == "ProcessIsolatedSentiment"
        assert svc.embedder.__class__.__name__ == "ProcessIsolatedEmbedder"
        assert svc.reranker.__class__.__name__ == "ProcessIsolatedReranker"
    finally:
        svc.close()


def test_process_isolated_service_can_process_capture(
    monkeypatch: pytest.MonkeyPatch,
    synth_capture: Callable[..., AwarenessCaptureView],
) -> None:
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_PROCESSES", "2")
    settings = Settings(mode=CatchemMode.PRODUCTION_SAFE)
    svc = build_service(settings)
    try:
        cap = synth_capture()
        rec = svc.process(cap)
        assert rec.capture_id == cap.capture_id
        assert rec.is_finance_relevant is True
        assert rec.model_versions["zero_shot"] == "stub-zero-shot/v1"
    finally:
        svc.close()


def test_delegate_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_PROCESSES", "1")
    settings = Settings(mode=CatchemMode.PRODUCTION_SAFE)
    svc = build_service(settings)
    try:
        # Call encode
        arr = svc.embedder.encode("hello")
        assert arr is not None
        
        # Call encode_many
        arrs = svc.embedder.encode_many(["hello", "world"])
        assert arrs is not None
        
        # Call rank
        ranked = svc.reranker.rank("query", ["candidate1", "candidate2"])
        assert len(ranked) == 2
    finally:
        svc.close()


def test_worker_errors(synth_capture: Callable[..., AwarenessCaptureView]) -> None:
    import catchem.service

    cap = synth_capture()

    catchem.service._worker_zero_shot = None
    with pytest.raises(RuntimeError, match="Worker zero_shot model not initialized"):
        _worker_classify_zero_shot(cap)

    catchem.service._worker_sentiment = None
    with pytest.raises(RuntimeError, match="Worker sentiment model not initialized"):
        _worker_classify_sentiment(cap)

    catchem.service._worker_embedder = None
    with pytest.raises(RuntimeError, match="Worker embedder model not initialized"):
        _worker_encode_embedding("test")

    with pytest.raises(RuntimeError, match="Worker embedder model not initialized"):
        _worker_encode_many_embedding(["test"])

    catchem.service._worker_reranker = None
    with pytest.raises(RuntimeError, match="Worker reranker model not initialized"):
        _worker_rank_reranker("test", ["test"])


def test_worker_success_paths_direct(synth_capture: Callable[..., AwarenessCaptureView]) -> None:
    tax = load_taxonomy(default_taxonomy_path())
    
    # Initialize the worker globals manually in the parent process
    _init_worker(
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        use_ml_stubs=True,
        taxonomy=tax,
        nice_value=12,
        memory_limit_mb=None,
    )
    
    cap = synth_capture()
    # Call directly in the parent process to get coverage on the success returns
    assert _worker_classify_zero_shot(cap) is not None
    assert _worker_classify_sentiment(cap) is not None
    assert _worker_encode_embedding("hello") is not None
    assert _worker_encode_many_embedding(["hello"]) is not None
    assert _worker_rank_reranker("hello", ["world"]) is not None


def test_init_worker_nice_error(monkeypatch: pytest.MonkeyPatch) -> None:
    if hasattr(os, "nice"):
        def mock_nice(val: int) -> int:
            raise OSError("mocked nice error")
        monkeypatch.setattr(os, "nice", mock_nice)

    tax = load_taxonomy(default_taxonomy_path())
    _init_worker(
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        use_ml_stubs=True,
        taxonomy=tax,
        nice_value=12,
        memory_limit_mb=None,
    )


def test_init_worker_no_nice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "nice", raising=False)

    tax = load_taxonomy(default_taxonomy_path())
    _init_worker(
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        use_ml_stubs=True,
        taxonomy=tax,
        nice_value=12,
        memory_limit_mb=None,
    )


def test_init_worker_resource_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_setrlimit(limit: int, val: tuple[int, int]) -> None:
        raise ValueError("mocked limit error")
    monkeypatch.setattr(resource, "setrlimit", mock_setrlimit)

    tax = load_taxonomy(default_taxonomy_path())
    _init_worker(
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        use_ml_stubs=True,
        taxonomy=tax,
        nice_value=12,
        memory_limit_mb=1024,
    )


def test_service_context_manager_and_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_ENABLED", "true")
    settings = Settings(mode=CatchemMode.PRODUCTION_SAFE)
    
    # test __enter__ / __exit__
    with build_service(settings) as svc:
        assert svc.use_isolation is True

    # test close exception handling
    svc = build_service(settings)
    def mock_shutdown(*args, **kwargs) -> None:
        raise RuntimeError("shutdown error")
    monkeypatch.setattr(svc._pool, "shutdown", mock_shutdown)
    svc.close()  # should swallow exception

    # test __del__ exception handling
    svc = build_service(settings)
    def mock_close() -> None:
        raise RuntimeError("close error")
    monkeypatch.setattr(svc, "close", mock_close)
    svc.__del__()  # should swallow exception


def test_fifo_cache_unit() -> None:
    # Test FIFOCache maxsize limits
    cache = FIFOCache(maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    assert cache.get("b") == 2
    
    # evict 'a' (FIFO)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    
    # duplicate key updates and doesn't evict
    cache.set("b", 22)
    assert cache.get("b") == 22
    
    # maxsize 0
    cache0 = FIFOCache(maxsize=0)
    cache0.set("a", 1)
    assert cache0.get("a") is None


def test_delegate_caching_and_partial_hits(
    monkeypatch: pytest.MonkeyPatch,
    synth_capture: Callable[..., AwarenessCaptureView],
) -> None:
    monkeypatch.setenv("CATCHEM_MODELS__ISOLATION_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_MODELS__CACHE_MAXSIZE", "3")
    settings = Settings(mode=CatchemMode.PRODUCTION_SAFE)
    svc = build_service(settings)
    
    try:
        # Zero-shot caching
        cap1 = synth_capture(capture_id="c1")
        res1 = svc.zero_shot.classify(cap1)
        assert svc.zero_shot._cache.get("c1") is res1
        # Call again, should hit cache
        res1_again = svc.zero_shot.classify(cap1)
        assert res1_again is res1
        
        # Sentiment caching
        s_res1 = svc.sentiment.classify(cap1)
        assert svc.sentiment._cache.get("c1") is s_res1
        s_res1_again = svc.sentiment.classify(cap1)
        assert s_res1_again is s_res1
        
        # Embedder caching
        e_res1 = svc.embedder.encode("hello")
        assert np.array_equal(svc.embedder._cache.get("hello"), e_res1)
        e_res1_again = svc.embedder.encode("hello")
        assert np.array_equal(e_res1_again, e_res1)
        
        # Reranker caching
        r_res1 = svc.reranker.rank("query", ["a", "b"])
        assert svc.reranker._cache.get(("query", ("a", "b"))) is r_res1
        r_res1_again = svc.reranker.rank("query", ["a", "b"])
        assert r_res1_again is r_res1
        
        # Embedder encode_many partial hits
        # Cache contains "hello" from above.
        # Now call encode_many with ["hello", "world"]
        em_res = svc.embedder.encode_many(["hello", "world"])
        assert len(em_res) == 2
        assert np.array_equal(em_res[0], e_res1)
        assert svc.embedder._cache.get("world") is not None
        
        # Test encode_many with ALL hits (covers if not missing_texts branch)
        em_hits = svc.embedder.encode_many(["hello", "world"])
        assert len(em_hits) == 2
        assert np.array_equal(em_hits[0], e_res1)

        # Empty encode_many
        empty_em = svc.embedder.encode_many([])
        assert empty_em.shape == (0, 64)
        
    finally:
        svc.close()


def test_process_isolated_service_non_stub_pool_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock ProcessPoolExecutor to avoid spawning real process pool during test coverage of the else branch
    import concurrent.futures
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor)
    
    settings = Settings(mode=CatchemMode.PRODUCTION_SAFE)
    # Force use_ml_stubs to False and isolation_enabled to True
    settings.models.use_ml_stubs = False
    settings.models.isolation_enabled = True
    
    # Mock make_* to return stub versions even when use_ml_stubs is False to prevent real HF loads
    import catchem.embeddings
    import catchem.reranker
    import catchem.sentiment
    import catchem.zero_shot_classifier
    monkeypatch.setattr(catchem.zero_shot_classifier, "make_zero_shot", lambda *args, **kwargs: catchem.zero_shot_classifier.make_zero_shot(args[0], args[1], use_ml_stubs=True))
    monkeypatch.setattr(catchem.sentiment, "make_sentiment", lambda *args, **kwargs: catchem.sentiment.make_sentiment(args[0], use_ml_stubs=True))
    monkeypatch.setattr(catchem.embeddings, "make_embedder", lambda *args, **kwargs: catchem.embeddings.make_embedder(args[0], use_ml_stubs=True))
    monkeypatch.setattr(catchem.reranker, "make_reranker", lambda *args, **kwargs: catchem.reranker.make_reranker(args[0], use_ml_stubs=True))
    
    svc = build_service(settings)
    try:
        assert svc.use_isolation is True
        assert svc._pool is not None
    finally:
        svc.close()


def test_resilient_executor_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import BrokenExecutor

    from catchem.service import ResilientExecutor
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        # Submit a simple job first
        fut = exec_proxy.submit(lambda x: x * 2, 5)
        assert fut.result() == 10

        # Now mock the underlying pool's submit to raise BrokenExecutor on the first call,
        # then return a working future on the second call.
        original_pool = exec_proxy._pool
        calls = 0

        def mock_submit(fn, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BrokenExecutor("Simulated broken executor on submit")
            return original_pool.submit(fn, *args, **kwargs)

        monkeypatch.setattr(original_pool, "submit", mock_submit)

        # Call submit: it should raise BrokenExecutor, recreate the pool, and succeed on the retry!
        fut2 = exec_proxy.submit(lambda x: x * 3, 5)
        assert fut2.result() == 15
        assert exec_proxy._pool is not original_pool
    finally:
        exec_proxy.shutdown()


def test_resilient_future_result_recovery() -> None:
    from concurrent.futures import BrokenExecutor

    from catchem.service import ResilientExecutor, ResilientFuture
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        class BadFuture:
            def result(self, timeout=None):
                raise BrokenExecutor("Future execution failed because worker died")

        original_pool = exec_proxy._pool
        res_fut = ResilientFuture(
            executor=exec_proxy,
            pool=original_pool,
            future=BadFuture(),
            fn=lambda x: x + 1,
            args=(10,),
            kwargs={},
        )
        val = res_fut.result()
        assert val == 11
        assert exec_proxy._pool is not original_pool
    finally:
        exec_proxy.shutdown()


def test_resilient_executor_propagates_double_failure() -> None:
    from concurrent.futures import BrokenExecutor

    from catchem.service import ResilientExecutor
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        def bad_submit(*args, **kwargs):
            raise BrokenExecutor("Always broken")

        original_recreate = exec_proxy.recreate_pool
        def mock_recreate(failed_pool=None):
            original_recreate(failed_pool)
            if exec_proxy._pool is not None:
                exec_proxy._pool.submit = bad_submit

        exec_proxy.recreate_pool = mock_recreate
        exec_proxy._pool.submit = bad_submit

        with pytest.raises(BrokenExecutor):
            exec_proxy.submit(lambda x: x, 1)
    finally:
        exec_proxy.shutdown()


def test_resilient_executor_recreate_ignores_stale() -> None:
    from catchem.service import ResilientExecutor
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        initial_pool = exec_proxy._pool

        # Recreate pool once specifying the initial pool - should succeed
        exec_proxy.recreate_pool(initial_pool)
        second_pool = exec_proxy._pool
        assert second_pool is not initial_pool

        # Recreate pool again specifying the initial pool (which is now stale) - should do nothing!
        exec_proxy.recreate_pool(initial_pool)
        assert exec_proxy._pool is second_pool

        # Recreate pool specifying None - should succeed
        exec_proxy.recreate_pool(None)
        assert exec_proxy._pool is not second_pool
    finally:
        exec_proxy.shutdown()


def test_resilient_executor_raises_when_closed() -> None:
    from catchem.service import ResilientExecutor
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    # Test double shutdown to cover self._pool is None branch
    exec_proxy.shutdown()
    exec_proxy.shutdown()

    # Test recreate_pool on closed executor
    exec_proxy.recreate_pool()

    with pytest.raises(RuntimeError, match="Executor is closed"):
        exec_proxy.submit(lambda x: x, 1)


def test_resilient_future_result_raises_when_executor_closed() -> None:
    from concurrent.futures import BrokenExecutor

    from catchem.service import ResilientExecutor, ResilientFuture
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        class BadFuture:
            def result(self, timeout=None):
                raise BrokenExecutor("die")

        original_pool = exec_proxy._pool
        
        # We hook recreate_pool to shutdown the executor during recovery!
        def mock_recreate(failed_pool=None):
            exec_proxy.shutdown()
        exec_proxy.recreate_pool = mock_recreate

        res_fut = ResilientFuture(
            executor=exec_proxy,
            pool=original_pool,
            future=BadFuture(),
            fn=lambda x: x,
            args=(1,),
            kwargs={},
        )
        with pytest.raises(RuntimeError, match="Executor is closed"):
            res_fut.result()
    finally:
        exec_proxy.shutdown()


def test_resilient_executor_submit_raises_when_executor_closed_midway() -> None:
    from concurrent.futures import BrokenExecutor

    from catchem.service import ResilientExecutor
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        # Force the first submit to fail
        original_pool = exec_proxy._pool
        def bad_submit(*args, **kwargs):
            raise BrokenExecutor("Always broken")
        original_pool.submit = bad_submit

        # Shutdown the executor during recovery
        def mock_recreate(failed_pool=None):
            exec_proxy.shutdown()
        exec_proxy.recreate_pool = mock_recreate

        with pytest.raises(RuntimeError, match="Executor is closed"):
            exec_proxy.submit(lambda x: x, 1)
    finally:
        exec_proxy.shutdown()


def test_resilient_executor_recreate_shutdown_error() -> None:
    from catchem.service import ResilientExecutor
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    exec_proxy = ResilientExecutor(
        use_stubs=True,
        max_workers=1,
        zero_shot_model_name="facebook/bart-large-mnli",
        sentiment_model_name="ProsusAI/finbert",
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        reranker_model_name="cross-encoder/ms-marco-MiniLM-L6-v2",
        taxonomy=tax,
        nice_value=0,
        memory_limit_mb=None,
    )
    try:
        def bad_shutdown(*args, **kwargs):
            raise RuntimeError("Shutdown failed")
        exec_proxy._pool.shutdown = bad_shutdown
        # Trigger recreate_pool which will call shutdown and swallow the RuntimeError!
        exec_proxy.recreate_pool()
    finally:
        exec_proxy.shutdown()


