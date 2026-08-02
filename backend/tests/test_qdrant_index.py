from types import SimpleNamespace

import pytest
from app.core.config import Settings
from app.core.context import TenantContext
from app.embeddings.qdrant_index import EmbeddingIndex, EmbeddingSpec, collection_name


class FakeQdrant:
    def __init__(self) -> None:
        self.exists = False
        self.created: list[dict[str, object]] = []
        self.upserts: list[dict[str, object]] = []
        self.queries: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []

    async def collection_exists(self, _: str) -> bool:
        return self.exists

    async def create_collection(self, *, collection_name: str, vectors_config: object) -> bool:
        self.exists = True
        self.created.append({"collection_name": collection_name, "vectors_config": vectors_config})
        return True

    async def upsert(self, **kwargs: object) -> None:
        self.upserts.append(kwargs)

    async def query_points(self, **kwargs: object) -> SimpleNamespace:
        self.queries.append(kwargs)
        return SimpleNamespace(points=[SimpleNamespace(id=7, score=0.9, payload={"user_id": 3})])

    async def delete(self, **kwargs: object) -> None:
        self.deletes.append(kwargs)


def test_collection_name_changes_when_embedding_version_changes() -> None:
    first = collection_name("tuxnews", EmbeddingSpec("model/a", "v1", 3))
    second = collection_name("tuxnews", EmbeddingSpec("model/a", "v2", 3))
    assert first != second
    assert first.startswith("tuxnews_model_a_")


@pytest.mark.asyncio
async def test_index_creates_versioned_collection_and_filters_by_user() -> None:
    fake = FakeQdrant()
    index = EmbeddingIndex(
        Settings(qdrant_collection_prefix="test"),
        client=fake,
        spec=EmbeddingSpec("test-model", "v7", 3),
    )
    await index.upsert(
        tenant=TenantContext(3),
        article_id=7,
        vector=[0.1, 0.2, 0.3],
        canonical_url_hash="a" * 64,
    )
    hits = await index.search(user_id=3, vector=[0.1, 0.2, 0.3], limit=5)

    assert len(fake.created) == 1
    assert fake.created[0]["collection_name"] == index.collection
    assert fake.upserts[0]["wait"] is True
    assert hits[0].article_id == 7
    assert fake.queries[0]["query_filter"] is not None
    await index.delete_user(user_id=3)
    assert fake.deletes[0]["wait"] is True
    assert fake.deletes[0]["points_selector"] is not None


@pytest.mark.asyncio
async def test_index_rejects_wrong_dimension_and_non_finite_values() -> None:
    index = EmbeddingIndex(Settings(), client=FakeQdrant(), spec=EmbeddingSpec("test-model", "v1", 2))

    with pytest.raises(ValueError, match="dimension"):
        await index.upsert(
            tenant=TenantContext(1),
            article_id=1,
            vector=[1.0],
            canonical_url_hash="b" * 64,
        )
    with pytest.raises(ValueError, match="non-finite"):
        await index.upsert(
            tenant=TenantContext(1),
            article_id=1,
            vector=[float("nan"), 1.0],
            canonical_url_hash="b" * 64,
        )
