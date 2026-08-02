from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.core.config import Settings, get_settings
from app.core.context import TenantContext
from app.observability import metrics


@dataclass(frozen=True)
class EmbeddingSpec:
    model: str
    version: str
    dimension: int


@dataclass(frozen=True)
class EmbeddingHit:
    article_id: int
    score: float
    payload: Mapping[str, Any]


def collection_name(prefix: str, spec: EmbeddingSpec) -> str:
    normalized_model = re.sub(r"[^a-zA-Z0-9]+", "_", spec.model).strip("_").lower()
    model_hash = hashlib.sha1(spec.model.encode("utf-8")).hexdigest()[:8]
    normalized_version = re.sub(r"[^a-zA-Z0-9]+", "_", spec.version).strip("_").lower()
    return f"{prefix}_{normalized_model}_{model_hash}_{normalized_version}"


class EmbeddingIndex:
    """Qdrant adapter whose collection can always be rebuilt from PostgreSQL."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: AsyncQdrantClient | Any | None = None,
        spec: EmbeddingSpec | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.spec = spec or EmbeddingSpec(
            model=self.settings.embedding_model,
            version=self.settings.embedding_version,
            dimension=self.settings.embedding_dimension,
        )
        self.collection = collection_name(self.settings.qdrant_collection_prefix, self.spec)
        self._owns_client = client is None
        self.client = client or AsyncQdrantClient(
            url=self.settings.qdrant_url,
            timeout=int(self.settings.http_timeout_seconds),
        )

    async def ensure_collection(self) -> str:
        timer = metrics.timer("qdrant.ensure_collection")
        try:
            if not await self.client.collection_exists(self.collection):
                await self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(size=self.spec.dimension, distance=models.Distance.COSINE),
                    metadata={
                        "embedding_model": self.spec.model,
                        "embedding_version": self.spec.version,
                        "embedding_dimension": self.spec.dimension,
                    },
                )
        except Exception:
            timer.finish(success=False)
            raise
        timer.finish(success=True)
        return self.collection

    def _validate_vector(self, vector: Sequence[float]) -> list[float]:
        if len(vector) != self.spec.dimension:
            raise ValueError("embedding dimension does not match collection")
        normalized = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError("embedding contains a non-finite value")
        return normalized

    async def upsert(
        self,
        *,
        tenant: TenantContext,
        article_id: int,
        vector: Sequence[float],
        canonical_url_hash: str,
    ) -> None:
        timer = metrics.timer("qdrant.upsert")
        try:
            await self.ensure_collection()
            await self.client.upsert(
                collection_name=self.collection,
                points=[
                    models.PointStruct(
                        id=article_id,
                        vector=self._validate_vector(vector),
                        payload={
                            "article_id": article_id,
                            "user_id": tenant.tenant_id,
                            "canonical_url_hash": canonical_url_hash,
                            "embedding_model": self.spec.model,
                            "embedding_version": self.spec.version,
                        },
                    )
                ],
                wait=True,
            )
        except Exception:
            timer.finish(success=False)
            raise
        timer.finish(success=True)

    async def search(
        self,
        *,
        user_id: int,
        vector: Sequence[float],
        limit: int = 20,
    ) -> tuple[EmbeddingHit, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        timer = metrics.timer("qdrant.search")
        try:
            await self.ensure_collection()
            query_filter = models.Filter(
                must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))]
            )
            response = await self.client.query_points(
                collection_name=self.collection,
                query=self._validate_vector(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            hits: list[EmbeddingHit] = []
            for point in response.points:
                payload = point.payload or {}
                if payload.get("user_id") != user_id:
                    continue
                hits.append(EmbeddingHit(article_id=int(point.id), score=float(point.score), payload=payload))
        except Exception:
            timer.finish(success=False)
            raise
        timer.finish(success=True)
        return tuple(hits)

    async def delete_user(self, *, user_id: int) -> None:
        timer = metrics.timer("qdrant.delete_user")
        try:
            if not await self.client.collection_exists(self.collection):
                timer.finish(success=True)
                return
            query_filter = models.Filter(
                must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))]
            )
            await self.client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(filter=query_filter),
                wait=True,
            )
        except Exception:
            timer.finish(success=False)
            raise
        timer.finish(success=True)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.close()
