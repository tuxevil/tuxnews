from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import (
    AccountActionResponse,
    EmailChangeConfirmRequest,
    EmailChangeRequest,
    InvitationAcceptRequest,
    LoginRequest,
    LogoutResponse,
    PasswordRecoveryConfirmRequest,
    PasswordRecoveryRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserPublic,
)
from app.audit.service import record_audit
from app.core.config import get_settings
from app.core.permissions import scopes_for_role
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    new_family_id,
    verify_password,
)
from app.db.models import User, UserSession
from app.db.session import get_session
from app.users.service import (
    accept_invitation,
    complete_email_change,
    complete_password_recovery,
    issue_email_change,
    issue_password_recovery,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        path="/api/v1/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(key=settings.refresh_cookie_name, path="/api/v1/auth")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _token_response(user: User) -> TokenResponse:
    settings = get_settings()
    scopes = sorted(scopes_for_role(user.role))
    return TokenResponse(
        access_token=create_access_token(user.id, scopes=scopes),
        expires_in=settings.access_token_minutes * 60,
        scopes=scopes,
        user=UserPublic.model_validate(user),
    )


async def _issue_tokens(
    response: Response,
    session: AsyncSession,
    user: User,
    *,
    correlation_id: str | None = None,
) -> TokenResponse:
    settings = get_settings()
    family_id = new_family_id()
    refresh_token = create_refresh_token(user.id, family_id)
    session.add(
        UserSession(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            family_id=family_id,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        )
    )
    await session.flush()
    session_record = await session.scalar(
        select(UserSession)
        .where(UserSession.user_id == user.id, UserSession.refresh_token_hash == hash_refresh_token(refresh_token))
    )
    if session_record is None:
        raise RuntimeError("session was not persisted")
    record_audit(
        session,
        user_id=None,
        action="auth.session.created",
        resource_type="user_session",
        resource_id=str(session_record.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type="user",
        actor_id=str(user.id),
        tenant_id=user.id,
    )
    await session.commit()
    _set_refresh_cookie(response, refresh_token)
    return _token_response(user)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    settings = get_settings()
    if not settings.allow_public_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="registration disabled")
    email = payload.email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")
    user = User(email=email, password_hash=hash_password(payload.password), role="user")
    session.add(user)
    try:
        await session.flush()
        record_audit(
            session,
            user_id=None,
            action="user.registered",
            resource_type="user",
            resource_id=str(user.id),
            outcome="success",
            correlation_id=getattr(request.state, "correlation_id", None),
            actor_type="user",
            actor_id=str(user.id),
            tenant_id=user.id,
        )
        return await _issue_tokens(
            response,
            session,
            user,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered") from exc


@router.post("/invitations/accept", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def accept_user_invitation(
    payload: InvitationAcceptRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    user = await accept_invitation(
        session,
        token=payload.token,
        password=payload.password,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid or expired invitation")
    return await _issue_tokens(
        response,
        session,
        user,
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@router.post("/password-recovery", response_model=AccountActionResponse, status_code=status.HTTP_202_ACCEPTED)
async def request_password_recovery(
    payload: PasswordRecoveryRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AccountActionResponse:
    user = await session.scalar(
        select(User).where(User.email == payload.email.lower(), User.is_active.is_(True), User.deleted_at.is_(None))
    )
    if user is not None:
        await issue_password_recovery(
            session,
            user_id=user.id,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    return AccountActionResponse()


@router.post("/password-recovery/confirm", response_model=AccountActionResponse)
async def confirm_password_recovery(
    payload: PasswordRecoveryConfirmRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AccountActionResponse:
    completed = await complete_password_recovery(
        session,
        token=payload.token,
        new_password=payload.new_password,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    if not completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid or expired account token")
    return AccountActionResponse()


@router.post("/email-change", response_model=AccountActionResponse, status_code=status.HTTP_202_ACCEPTED)
async def request_email_change(
    payload: EmailChangeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AccountActionResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    try:
        await issue_email_change(
            session,
            user_id=user.id,
            new_email=str(payload.new_email),
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AccountActionResponse()


@router.post("/email-change/confirm", response_model=AccountActionResponse)
async def confirm_email_change(
    payload: EmailChangeConfirmRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AccountActionResponse:
    completed = await complete_email_change(
        session,
        token=payload.token,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    if not completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid or expired account token")
    return AccountActionResponse()


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    return await _issue_tokens(
        response,
        session,
        user,
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    settings = get_settings()
    refresh_token = (payload.refresh_token if payload else None) or request.cookies.get(settings.refresh_cookie_name)
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token required")
    try:
        token_data = decode_token(refresh_token, "refresh")
        user_id = int(token_data["sub"])
        token_hash = hash_refresh_token(refresh_token)
    except (ValueError, KeyError, TypeError, jwt.InvalidTokenError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token") from exc
    stored = await session.scalar(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash).with_for_update()
    )
    if stored is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    if stored.revoked_at is not None:
        # A rotated token was reused. Revoke every token in the family, including
        # the replacement that may otherwise still be valid.
        await session.execute(
            update(UserSession)
            .where(UserSession.family_id == stored.family_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        record_audit(
            session,
            user_id=None,
            action="auth.refresh.reuse_detected",
            resource_type="user_session",
            resource_id=str(stored.id),
            outcome="failure",
            correlation_id=getattr(request.state, "correlation_id", None),
            actor_type="user",
            actor_id=str(user_id),
            tenant_id=user_id,
            details={"family_id": stored.family_id},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    if _as_utc(stored.expires_at) <= datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    if stored.user_id != user_id or stored.family_id != token_data.get("family_id"):
        stored.revoked_at = datetime.now(UTC)
        record_audit(
            session,
            user_id=None,
            action="auth.refresh.invalid",
            resource_type="user_session",
            resource_id=str(stored.id),
            outcome="failure",
            correlation_id=getattr(request.state, "correlation_id", None),
            actor_type="user",
            actor_id=str(user_id),
            tenant_id=stored.user_id,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
    user = await session.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    stored.revoked_at = datetime.now(UTC)
    await session.flush()
    new_refresh = create_refresh_token(user.id, stored.family_id)
    stored_new = UserSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(new_refresh),
        family_id=stored.family_id,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    session.add(stored_new)
    await session.flush()
    record_audit(
        session,
        user_id=None,
        action="auth.session.refreshed",
        resource_type="user_session",
        resource_id=str(stored_new.id),
        outcome="success",
        correlation_id=getattr(request.state, "correlation_id", None),
        actor_type="user",
        actor_id=str(user.id),
        tenant_id=user.id,
        details={"replaced_session_id": stored.id},
    )
    await session.commit()
    _set_refresh_cookie(response, new_refresh)
    return _token_response(user)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> LogoutResponse:
    settings = get_settings()
    refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if refresh_token:
        stored = await session.scalar(
            select(UserSession).where(UserSession.refresh_token_hash == hash_refresh_token(refresh_token))
        )
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = datetime.now(UTC)
            record_audit(
                session,
                user_id=None,
                action="auth.session.revoked",
                resource_type="user_session",
                resource_id=str(stored.id),
                outcome="success",
                correlation_id=getattr(request.state, "correlation_id", None),
                actor_type="user",
                actor_id=str(stored.user_id),
                tenant_id=stored.user_id,
            )
            await session.commit()
    _clear_refresh_cookie(response)
    return LogoutResponse()


@router.get("/me", response_model=UserPublic)
async def me(user: User = Depends(get_current_user)) -> UserPublic:
    return UserPublic.model_validate(user)
