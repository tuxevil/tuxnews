from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.api.schemas import (
    AdminUserUpdateRequest,
    InvitationCreated,
    InvitationCreateRequest,
    UserAdminPublic,
)
from app.core.permissions import Scope
from app.db.models import User
from app.db.session import get_session
from app.users.service import (
    create_invitation,
    delete_user,
    get_user,
    list_users,
    reactivate_user,
    suspend_user,
    update_user,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


def _public(user: User) -> UserAdminPublic:
    return UserAdminPublic.model_validate(user)


@router.get("/users", response_model=list[UserAdminPublic])
async def get_users(
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> list[UserAdminPublic]:
    return [_public(user) for user in await list_users(session)]


@router.get("/users/{user_id}", response_model=UserAdminPublic)
async def get_user_by_id(
    user_id: int,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> UserAdminPublic:
    del identity
    user = await get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _public(user)


@router.post("/invitations", response_model=InvitationCreated, status_code=status.HTTP_201_CREATED)
async def create_user_invitation(
    payload: InvitationCreateRequest,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> InvitationCreated:
    try:
        invitation, token = await create_invitation(
            session,
            invited_by_user_id=identity.user.id,
            email=str(payload.email),
            role=payload.role,
            expires_in_hours=payload.expires_in_hours,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        status_code = status.HTTP_409_CONFLICT if "registered" in str(exc) else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return InvitationCreated.model_validate(
        {
            "id": invitation.id,
            "role": invitation.role,
            "expires_at": invitation.expires_at,
            "token": token,
        }
    )


@router.patch("/users/{user_id}", response_model=UserAdminPublic)
async def update_managed_user(
    user_id: int,
    payload: AdminUserUpdateRequest,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> UserAdminPublic:
    if payload.role is None and payload.is_active is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="no user changes supplied")
    try:
        user = await update_user(
            session,
            actor_id=identity.user.id,
            user_id=user_id,
            role=payload.role,
            is_active=payload.is_active,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _public(user)


@router.post("/users/{user_id}/suspend", response_model=UserAdminPublic)
async def suspend_managed_user(
    user_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> UserAdminPublic:
    try:
        user = await suspend_user(
            session,
            actor_id=identity.user.id,
            user_id=user_id,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _public(user)


@router.post("/users/{user_id}/reactivate", response_model=UserAdminPublic)
async def reactivate_managed_user(
    user_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> UserAdminPublic:
    try:
        user = await reactivate_user(
            session,
            actor_id=identity.user.id,
            user_id=user_id,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _public(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_managed_user(
    user_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        deleted = await delete_user(
            session,
            actor_id=identity.user.id,
            user_id=user_id,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
