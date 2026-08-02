import pytest
from app.core.config import Settings
from app.embeddings.provider import EmbeddingUnavailable, SentenceTransformerProvider


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def encode(self, text: str, **kwargs: object) -> list[float]:
        self.calls.append({"text": text, **kwargs})
        return [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_sentence_transformer_provider_loads_lazily_and_validates_vector() -> None:
    model = FakeModel()
    provider = SentenceTransformerProvider(
        Settings(embedding_model="test-model", embedding_dimension=3),
        model_factory=lambda name: model,
    )

    await provider.ensure_available()
    vector = await provider.embed("  Article   content  ")

    assert vector == [0.1, 0.2, 0.3]
    assert model.calls[0]["text"] == "Article content"
    assert model.calls[0]["normalize_embeddings"] is True


@pytest.mark.asyncio
async def test_sentence_transformer_provider_reports_unavailable_model() -> None:
    provider = SentenceTransformerProvider(
        Settings(embedding_dimension=3),
        model_factory=lambda _: (_ for _ in ()).throw(RuntimeError("missing model")),
    )

    with pytest.raises(EmbeddingUnavailable, match="could not be loaded"):
        await provider.ensure_available()


@pytest.mark.asyncio
async def test_sentence_transformer_provider_rejects_wrong_dimension() -> None:
    class WrongModel(FakeModel):
        def encode(self, text: str, **kwargs: object) -> list[float]:
            return [0.1, 0.2]

    provider = SentenceTransformerProvider(
        Settings(embedding_dimension=3),
        model_factory=lambda _: WrongModel(),
    )

    with pytest.raises(EmbeddingUnavailable, match="invalid dimension"):
        await provider.embed("content")
