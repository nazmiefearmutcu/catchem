"""Stage D: sentence embeddings + a tiny local vector cache.

Stub uses a hashed-feature representation (no ML deps). Production swaps to
sentence-transformers/all-MiniLM-L6-v2.

The cache is a simple ``.npy`` per-record file under ``vector_index_dir`` plus a
SQLite-backed manifest in Storage (not added here to keep this module
self-contained — vectors live as files keyed by capture_id).
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np


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
        toks = [t for t in text.lower().split() if any(c.isalpha() for c in t)]
        if not toks:
            return arr
        for t in toks:
            h = cls._hash_token(t)
            idx = h % cls.DIM
            sign = 1.0 if (h >> 32) & 1 else -1.0
            arr[idx] += sign
        # Also add bigram features for stronger phrase signal
        for a, b in zip(toks, toks[1:]):
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
    """Tiny on-disk vector cache. capture_id → vector (.npy)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, capture_id: str, vec: np.ndarray) -> None:
        path = self.root / f"{capture_id}.npy"
        np.save(path, vec.astype(np.float32))

    def load(self, capture_id: str) -> np.ndarray | None:
        path = self.root / f"{capture_id}.npy"
        if not path.exists():
            return None
        return np.load(path)

    def nearest(self, vec: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        for p in sorted(self.root.glob("*.npy")):
            other = np.load(p)
            results.append((p.stem, cosine(vec, other)))
        results.sort(key=lambda kv: -kv[1])
        return results[:k]
