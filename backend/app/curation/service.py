from __future__ import annotations

from dataclasses import dataclass

from app.ai.gateway import LLMGateway, LLMProfile, LLMResponse, deterministic_fallback
from app.curation.schemas import CurationResult, validate_curation_output


@dataclass(frozen=True)
class CurationOutcome:
    result: CurationResult | None
    fallback_summary: str
    used_fallback: bool
    rejected: bool
    reason: str | None


class CurationService:
    def __init__(self, gateway: LLMGateway | None = None) -> None:
        self.gateway = gateway or LLMGateway()

    async def curate(
        self,
        *,
        title: str,
        content: str,
        profile: LLMProfile | str | None = None,
        use_llm: bool = True,
    ) -> CurationOutcome:
        if not use_llm:
            return CurationOutcome(None, deterministic_fallback(content), True, False, "hybrid_fallback")
        response: LLMResponse = await self.gateway.complete(
            instruction=(
                "Return only a JSON object matching the supplied schema. "
                "Create a factual, neutral title, summary, tags, reading time, and relevance score."
            ),
            external_data=f"<article_title>{title}</article_title>\n{content}",
            profile=profile,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "article_curation",
                    "strict": True,
                    "schema": CurationResult.model_json_schema(),
                },
            },
        )
        fallback_summary = deterministic_fallback(content)
        if response.used_fallback:
            return CurationOutcome(None, fallback_summary, True, False, response.error)
        try:
            result = validate_curation_output(response.content)
        except ValueError as exc:
            return CurationOutcome(None, fallback_summary, True, True, str(exc))
        return CurationOutcome(result, fallback_summary, False, False, None)
