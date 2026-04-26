from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Embedder(ABC):
    """Minimal embedding interface used by the indexer and search."""

    dim: int
    name: str

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (N, dim) float32 array. Must be L2-normalized."""

    @abstractmethod
    def embed_query(self, text: str) -> np.ndarray:
        """Return a (dim,) float32 array. Must be L2-normalized."""


class NoEmbedder(Embedder):
    """Sentinel used when the configured embedder can't be loaded.

    Indexing proceeds (chunks are stored as text) but no vector column is
    populated. Search falls back to LIKE matching.
    """

    name = "none"
    dim = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        raise RuntimeError("NoEmbedder cannot produce embeddings")

    def embed_query(self, text: str) -> np.ndarray:
        raise RuntimeError("NoEmbedder cannot produce embeddings")


class FastembedEmbedder(Embedder):
    """Wraps fastembed's ONNX text embedding models.

    The underlying model is downloaded on first use (~100-150MB) and cached
    under $HOME/.cache/fastembed. Subsequent runs are offline.
    """

    def __init__(self, model_name: str, dim: int):
        from fastembed import TextEmbedding  # type: ignore

        self._model = TextEmbedding(model_name=model_name)
        self.name = model_name
        self.dim = dim

    @staticmethod
    def _stack(vectors) -> np.ndarray:
        arr = np.asarray(list(vectors), dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return arr / norms

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._stack(self._model.embed(texts))

    def embed_query(self, text: str) -> np.ndarray:
        arr = self._stack(self._model.query_embed([text]))
        return arr[0]


def make_embedder(kind: str, model_name: str, dim: int) -> Embedder:
    """Factory that never raises: any failure falls back to NoEmbedder."""
    if kind == "none":
        return NoEmbedder()
    if kind == "fastembed":
        try:
            return FastembedEmbedder(model_name=model_name, dim=dim)
        except Exception:
            return NoEmbedder()
    return NoEmbedder()
