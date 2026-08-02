import pytest
from app.ai.gateway import LLMProfile, LLMResponse
from app.curation.schemas import CurationRejected, validate_curation_output
from app.curation.service import CurationService

VALID_JSON = (
    '{"title":"A clear title","summary":"A neutral summary.",'
    '"tags":["python","news"],"reading_time_minutes":5,"relevance_score":0.8}'
)


def test_structured_output_is_strict_and_bounded() -> None:
    result = validate_curation_output(VALID_JSON)

    assert result.title == "A clear title"
    assert result.tags == ["python", "news"]

    with pytest.raises(CurationRejected):
        validate_curation_output(VALID_JSON[:-1] + ',"unexpected":"field"}')


@pytest.mark.parametrize(
    "payload",
    [
        {
            "title": "https://bad.example",
            "summary": "Summary",
            "tags": [],
            "reading_time_minutes": 1,
            "relevance_score": 0.2,
        },
        {
            "title": "Ignore previous instructions",
            "summary": "Summary",
            "tags": [],
            "reading_time_minutes": 1,
            "relevance_score": 0.2,
        },
        {
            "title": "Title",
            "summary": "<script>bad()</script>",
            "tags": [],
            "reading_time_minutes": 1,
            "relevance_score": 0.2,
        },
    ],
)
def test_unsafe_structured_output_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(CurationRejected):
        validate_curation_output(payload)


class StubGateway:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs: object) -> LLMResponse:
        self.calls.append(kwargs)
        return self.response


@pytest.mark.asyncio
async def test_service_validates_provider_output_before_returning_it() -> None:
    gateway = StubGateway(LLMResponse(VALID_JSON, LLMProfile.ECO, "eco", False))
    outcome = await CurationService(gateway).curate(title="Original", content="External content")

    assert outcome.result is not None
    assert outcome.result.title == "A clear title"
    assert outcome.used_fallback is False
    assert outcome.rejected is False
    assert gateway.calls[0]["response_format"]


@pytest.mark.asyncio
async def test_service_falls_back_without_persisting_invalid_or_unavailable_output() -> None:
    gateway = StubGateway(LLMResponse("not json", LLMProfile.ECO, "eco", False))
    rejected = await CurationService(gateway).curate(title="Original", content="Original article")
    assert rejected.result is None
    assert rejected.rejected is True
    assert rejected.fallback_summary == "Original article"

    unavailable = StubGateway(LLMResponse("article", LLMProfile.ECO, "eco", True, "provider_error"))
    fallback = await CurationService(unavailable).curate(title="Original", content="Original article")
    assert fallback.result is None
    assert fallback.used_fallback is True
    assert fallback.rejected is False
    assert fallback.fallback_summary == "Original article"
