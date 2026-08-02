from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, get_owned_or_404, ownership_filter, require_scope
from app.api.schemas import SourceCreate, SourcePublic, SourceUpdate
from app.audit.service import record_audit
from app.core.permissions import Scope
from app.db.models import Source
from app.db.session import get_session
from app.ingestion.http_client import HttpFetchError, SafeHttpClient
from app.ingestion.queue import enqueue_source_ingestion

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


class IngestionTriggered(BaseModel):
    run_id: int
    status: str = "queued"


async def _validate_source_url(url: HttpUrl) -> str:
    normalized = str(url)
    try:
        async with SafeHttpClient() as client:
            await client.validate_destination(normalized)
    except HttpFetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="source URL is not allowed",
        ) from exc
    return normalized


def _source_response(source: Source) -> SourcePublic:
    return SourcePublic.model_validate(source)


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


@router.get("", response_model=list[SourcePublic])
async def list_sources(
    identity: IdentityContext = Depends(require_scope(Scope.SOURCES_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> list[SourcePublic]:
    sources = await session.scalars(
        select(Source).where(ownership_filter(Source, identity)).order_by(Source.id)
    )
    return [_source_response(source) for source in sources]


@router.get("/{source_id}", response_model=SourcePublic)
async def get_source(
    source_id: int,
    identity: IdentityContext = Depends(require_scope(Scope.SOURCES_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> SourcePublic:
    source = await get_owned_or_404(session, Source, source_id, identity)
    return _source_response(source)


@router.post("", response_model=SourcePublic, status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: SourceCreate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.SOURCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> SourcePublic:
    tenant_id = identity.tenant.tenant_id
    actor = identity.actor
    source = Source(
        user_id=tenant_id,
        name=payload.name.strip(),
        url=await _validate_source_url(payload.url),
        source_type=payload.source_type,
        tags=payload.tags,
        is_active=payload.is_active,
        origin="dynamic",
    )
    session.add(source)
    try:
        await session.flush()
        record_audit(
            session,
            user_id=tenant_id,
            action="source.created",
            resource_type="source",
            resource_id=str(source.id),
            outcome="success",
            correlation_id=_correlation_id(request),
            actor=actor,
            details={"url": source.url, "origin": source.origin},
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        record_audit(
            session,
            user_id=tenant_id,
            action="source.create_failed",
            resource_type="source",
            resource_id=None,
            outcome="failure",
            correlation_id=_correlation_id(request),
            actor=actor,
            details={"url": str(payload.url), "error": "unique_constraint"},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="source URL already exists") from exc
    await session.refresh(source)
    return _source_response(source)


@router.patch("/{source_id}", response_model=SourcePublic)
async def update_source(
    source_id: int,
    payload: SourceUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.SOURCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> SourcePublic:
    source = await get_owned_or_404(session, Source, source_id, identity)
    if source.origin == "static":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="static source is managed by sources.yaml")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="no source changes provided")
    if "url" in changes:
        changes["url"] = await _validate_source_url(changes["url"])
    for field, value in changes.items():
        setattr(source, field, value.strip() if field == "name" else value)
    record_audit(
        session,
        user_id=identity.tenant.tenant_id,
        action="source.updated",
        resource_type="source",
        resource_id=str(source.id),
        outcome="success",
        correlation_id=_correlation_id(request),
        actor=identity.actor,
        details={"fields": sorted(changes)},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="source URL already exists") from exc
    await session.refresh(source)
    return _source_response(source)


@router.post("/{source_id}/ingest", response_model=IngestionTriggered, status_code=status.HTTP_202_ACCEPTED)
async def trigger_source_ingestion(
    source_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.SOURCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> IngestionTriggered:
    source = await get_owned_or_404(session, Source, source_id, identity)
    run_id = await enqueue_source_ingestion(session, source=source, actor=identity.actor)
    record_audit(
        session,
        user_id=identity.tenant.tenant_id,
        action="ingestion.triggered",
        resource_type="ingestion_run",
        resource_id=str(run_id),
        outcome="success",
        correlation_id=_correlation_id(request),
        actor=identity.actor,
        details={"source_id": source.id},
    )
    await session.commit()
    return IngestionTriggered(run_id=run_id)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.SOURCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    source = await get_owned_or_404(session, Source, source_id, identity)
    if source.origin == "static":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="static source is managed by sources.yaml")
    source.is_active = False
    record_audit(
        session,
        user_id=identity.tenant.tenant_id,
        action="source.deactivated",
        resource_type="source",
        resource_id=str(source.id),
        outcome="success",
        correlation_id=_correlation_id(request),
        actor=identity.actor,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
