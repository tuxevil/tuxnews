from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.articles.lifecycle import publish_extracted_articles
from app.audit.service import record_audit
from app.core.permissions import Scope
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/admin/dev", tags=["dev"])


class DevPublishResult(BaseModel):
    published: int


@router.post("/publish-extracted", response_model=DevPublishResult)
async def publish_extracted(
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> DevPublishResult:
    """Dev-only helper: fast-forward extracted articles to published.

    Production curation runs through the real pipeline; this endpoint exists so
    a local deployment can see ingested content in the feed without a curator.
    """
    published = await publish_extracted_articles(identity.tenant.tenant_id)
    record_audit(
        session,
        user_id=identity.tenant.tenant_id,
        action="dev.articles.published",
        resource_type="article",
        resource_id="*",
        outcome="success",
        correlation_id=getattr(request.state, "correlation_id", None),
        actor=identity.actor,
        details={"published": published},
    )
    await session.commit()
    return DevPublishResult(published=published)
