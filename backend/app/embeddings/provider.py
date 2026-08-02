from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.embeddings.qdrant_index import EmbeddingSpec


class EmbeddingUnavailable(RuntimeError):
    """Raised when the configured local embedding model cannot be used."""


class EmbeddingProvider(Protocol):
    spec: EmbeddingSpec

    async def embed(self, text: str) -> list[float]: ...


ModelFactory = Callable[[str], Any]


class SentenceTransformerProvider:
    """Lazy CPU provider for the PRD's local all-MiniLM embedding model."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.spec = EmbeddingSpec(
            model=self.settings.embedding_model,
            version=self.settings.embedding_version,
            dimension=self.settings.embedding_dimension,
        )
        self._model_factory = model_factory
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            try:
                self._model = self._model_factory(self.spec.model)
            except Exception as exc:
                raise EmbeddingUnavailable("embedding model could not be loaded") from exc
            return self._model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:
            raise EmbeddingUnavailable("sentence-transformers is not installed") from exc
        try:
            self._model = SentenceTransformer(self.spec.model, device="cpu")
        except Exception as exc:
            raise EmbeddingUnavailable("embedding model could not be loaded") from exc
        return self._model

    def _encode_sync(self, text: str) -> list[float]:
        model = self._load_model()
        result = model.encode(
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        values = result.tolist() if hasattr(result, "tolist") else result
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise EmbeddingUnavailable("embedding provider returned an invalid vector")
        vector = [float(value) for value in values]
        if len(vector) != self.spec.dimension or not all(math.isfinite(value) for value in vector):
            raise EmbeddingUnavailable("embedding provider returned an invalid dimension")
        return vector

    async def ensure_available(self) -> None:
        await asyncio.to_thread(self._load_model)

    async def embed(self, text: str) -> list[float]:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            raise EmbeddingUnavailable("cannot embed empty text")
        return await asyncio.to_thread(self._encode_sync, normalized[:12_000])
