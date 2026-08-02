from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.clustering.service import ClusterView, list_cluster_views
from app.core.permissions import Scope
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/clusters", tags=["clusters"])


class ClusterItemPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    article_id: int
    title: str
    url: HttpUrl
    source_id: int
    source_name: str
    summary: str | None
    tags: list[str]
    published_at: datetime | None
    status: str
    similarity_score: float = Field(ge=0, le=1)
    membership_reason: str


class ClusterPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    summary: str | None
    status: str
    curation_state: str
    algorithm_version: str
    window_start: datetime | None
    window_end: datetime | None
    reconciled_at: datetime | None
    item_count: int
    source_count: int
    items: list[ClusterItemPublic]


def _cluster_response(view: ClusterView) -> ClusterPublic:
    return ClusterPublic.model_validate(view)


@router.get("", response_model=list[ClusterPublic])
async def list_clusters(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> list[ClusterPublic]:
    views = await list_cluster_views(session, tenant=identity.tenant)
    return [_cluster_response(view) for view in views]


@router.get("/{cluster_id}", response_model=ClusterPublic)
async def get_cluster(
    cluster_id: int,
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> ClusterPublic:
    views = await list_cluster_views(
        session,
        tenant=identity.tenant,
        cluster_id=cluster_id,
    )
    if not views:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    return _cluster_response(views[0])
