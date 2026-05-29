"""Stage D: sentence embeddings + a tiny local vector cache.

Stub uses a hashed-feature representation (no ML deps). Production swaps to
sentence-transformers/all-MiniLM-L6-v2.

The cache is a simple ``.npy`` per-record file under ``vector_index_dir`` plus a
SQLite-backed manifest in Storage (not added here to keep this module
self-contained — vectors live as files keyed by capture_id).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import numpy as np

# Alphanumeric-run tokenizer. Pre-fix `text.lower().split()` retained
# punctuation on every token, so "fed," and "fed" hashed to different
# buckets — the embedding lost most of its signal whenever publishers
# included commas/periods adjacent to words (i.e. always).
_TOKEN_RE = re.compile(r"[a-z0-9]+")


EMBED_DIM_STUB = 64   # blake2b max digest size


class Embedder(Protocol):
    @property
    def model_version(self) -> str: ...
    def encode(self, text: str) -> np.ndarray: ...
    def encode_many(self, texts: Iterable[str]) -> np.ndarray: ...


class EmbedderStub:
    """Deterministic, pure feature-hashing embedding (the hashing-trick).

    No random per-text bytes — every dimension is a stable function of token
    presence. That gives clean cosine geometry: near-dupes high, off-topic low.
    """

    model_version = "stub-embed/v2"
    DIM = EMBED_DIM_STUB

    @staticmethod
    def _hash_token(token: str) -> int:
        # Stable across processes (Python's built-in hash is randomized).
        digest = hashlib.blake2b(token.encode("utf-8", errors="ignore"), digest_size=8).digest()
        return int.from_bytes(digest, "little")

    @classmethod
    def _vec(cls, text: str) -> np.ndarray:
        arr = np.zeros(cls.DIM, dtype=np.float32)
        if not text:
            return arr
        # Alphanumeric-only tokens — strips punctuation so "fed," and "fed"
        # produce the same hash bucket.
        toks = _TOKEN_RE.findall(text.lower())
        if not toks:
            return arr
        for t in toks:
            h = cls._hash_token(t)
            idx = h % cls.DIM
            sign = 1.0 if (h >> 32) & 1 else -1.0
            arr[idx] += sign
        # Also add bigram features for stronger phrase signal.
        # `itertools.pairwise` yields (toks[0], toks[1]), (toks[1], toks[2]), ...
        # so the last token has no pair (same shape as the old zip-with-slice).
        for a, b in pairwise(toks):
            h = cls._hash_token(a + " " + b)
            idx = h % cls.DIM
            sign = 1.0 if (h >> 32) & 1 else -1.0
            arr[idx] += 0.5 * sign
        norm = float(np.linalg.norm(arr)) or 1.0
        return arr / norm

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text or "")

    def encode_many(self, texts: Iterable[str]) -> np.ndarray:
        return np.stack([self.encode(t) for t in texts]) if texts else np.zeros((0, EMBED_DIM_STUB), dtype=np.float32)


class EmbedderModel:
    """Wraps sentence-transformers. Lazy import."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        self.model_name = model_name
        self._model = SentenceTransformer(model_name, device="cpu")

    @property
    def model_version(self) -> str:
        return f"hf:{self.model_name}"

    def encode(self, text: str) -> np.ndarray:
        return self._model.encode(text or "", normalize_embeddings=True)

    def encode_many(self, texts: Iterable[str]) -> np.ndarray:
        return self._model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True)


def make_embedder(model_name: str, use_stub: bool) -> Embedder:
    if use_stub:
        return EmbedderStub()
    try:
        return EmbedderModel(model_name=model_name)
    except Exception:
        return EmbedderStub()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a)) or 1.0
    nb = float(np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / (na * nb))


class VectorIndex:
    """On-disk vector cache with an in-memory hot layer.

    BUG-DD: the previous `nearest()` re-read every `.npy` from disk on
    every query. At 10k records that is 10k disk seeks per call — fine for
    the test suite, painful in production. The in-memory `_cache` is
    populated on `save` and lazily filled by `load`/`nearest` so subsequent
    queries are pure-memory cosine sweeps.
    """

    # In-memory hot layer cap. Each MiniLM vector is 384 float32 = ~1.5 KB, so
    # 50k vectors ≈ 75 MB — a generous ceiling that bounds memory on a
    # long-running sidecar (save() adds one per ingest, forever) without the
    # LRU thrashing nearest() at realistic local volumes. Past the cap the
    # oldest cached vector is evicted; nearest()/load() simply re-read it from
    # the durable .npy on a miss.
    _CACHE_CAP = 50_000

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # OrderedDict + lock: `save()` runs in the news-poller's 4 ingest worker
        # threads (asyncio.to_thread) AND the WS-push reader, while `nearest()`
        # may sweep the cache from an API thread. A plain dict mutated by one
        # thread while another iterates it raises "dictionary changed size
        # during iteration" (or silently drops a write). Every cache access goes
        # through the lock; the OrderedDict also gives us LRU eviction.
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()

    def _cache_put(self, capture_id: str, vec: np.ndarray) -> None:
        """Insert/refresh a cache entry under the lock, evicting LRU past cap."""
        with self._cache_lock:
            self._cache[capture_id] = vec
            self._cache.move_to_end(capture_id)
            while len(self._cache) > self._CACHE_CAP:
                self._cache.popitem(last=False)

    def save(self, capture_id: str, vec: np.ndarray) -> None:
        path = self.root / f"{capture_id}.npy"
        as_f32 = vec.astype(np.float32)
        # Atomic publish: np.save() writes incrementally, so a concurrent
        # nearest()/load() doing np.load() on the same path can read a
        # half-written file ("could only read 0 elements") under the 4-thread
        # ingest. Write to a unique temp file whose suffix is OUTSIDE the
        # `*.npy` glob (so nearest() can't pick it up mid-write), then
        # os.replace() — atomic on POSIX — so readers see either the old file
        # or the complete new one, never a torn write. np.save needs a file
        # HANDLE here; given a bare path it would append a second ".npy".
        fd, tmp_name = tempfile.mkstemp(dir=self.root, suffix=".npytmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                np.save(fh, as_f32)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        # Cache the write so the next nearest() doesn't have to re-read it
        # from disk just to score it.
        self._cache_put(capture_id, as_f32)

    def load(self, capture_id: str) -> np.ndarray | None:
        with self._cache_lock:
            cached = self._cache.get(capture_id)
            if cached is not None:
                self._cache.move_to_end(capture_id)  # mark as recently used
                return cached
        # Cache miss → read the durable copy outside the lock (np.load is slow);
        # a concurrent duplicate load just re-puts the same vector (idempotent).
        path = self.root / f"{capture_id}.npy"
        if not path.exists():
            return None
        vec = np.load(path)
        self._cache_put(capture_id, vec)
        return vec

    def nearest(self, vec: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        # Lazy-load any vectors on disk that haven't entered the cache yet.
        # First query pays the disk cost; subsequent queries are pure RAM.
        for p in sorted(self.root.glob("*.npy")):
            cid = p.stem
            with self._cache_lock:
                present = cid in self._cache
            if not present:
                self._cache_put(cid, np.load(p))
        # Snapshot the items under the lock (cheap) so the O(n) cosine sweep
        # below iterates an immutable copy — a concurrent save()/load() can't
        # mutate the dict mid-iteration, and we don't hold the lock across the
        # expensive scoring (which would serialize ingest).
        with self._cache_lock:
            snapshot = list(self._cache.items())
        results = [(cid, cosine(vec, other)) for cid, other in snapshot]
        results.sort(key=lambda kv: -kv[1])
        return results[:k]
