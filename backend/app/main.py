import logging
import re
import time
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from redis.asyncio import Redis
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from app.api.routes.agent_tokens import router as agent_tokens_router
from app.api.routes.alerts import router as alerts_router
from app.api.routes.audit import router as audit_router
from app.api.routes.auth import router as auth_router
from app.api.routes.briefings import router as briefings_router
from app.api.routes.clusters import router as clusters_router
from app.api.routes.devtools import router as devtools_router
from app.api.routes.feed import router as feed_router
from app.api.routes.feedback import router as feedback_router
from app.api.routes.health import router as health_router
from app.api.routes.preferences import router as preferences_router
from app.api.routes.sources import router as sources_router
from app.api.routes.telemetry import router as telemetry_router
from app.api.routes.usage import router as usage_router
from app.api.routes.users import router as users_router
from app.core.config import get_settings
from app.core.permissions import Scope
from app.core.quota import QuotaService
from app.core.rate_limit import RateLimitDecision, RedisRateLimiter
from app.mcp.server import MCP_MOUNT_PATH, mcp_http_app
from app.observability import bind_context, log_event, metrics, normalize_operation, reset_context

logger = logging.getLogger("tuxnews.http")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

COMMON_ERROR_RESPONSES: dict[str, dict[str, str]] = {
    "401": {"description": "Missing or expired bearer token"},
    "403": {"description": "The token lacks a required scope for this operation"},
    "429": {"description": "Rate limit or quota exceeded; retry after the reset window"},
    "503": {"description": "A dependency service (database, Redis, Qdrant) is unavailable"},
}


def _annotate_openapi(document: dict[str, Any]) -> dict[str, Any]:
    """Document bearer scopes and common error responses for protected operations."""
    components = document.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    bearer = schemes.get("HTTPBearer")
    if isinstance(bearer, dict):
        bearer.setdefault(
            "scopes",
            {scope.value: scope.value for scope in Scope},
        )
    for _path, item in document.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for operation in item.values():
            if not isinstance(operation, dict) or not operation.get("security"):
                continue
            responses = operation.setdefault("responses", {})
            for code, payload in COMMON_ERROR_RESPONSES.items():
                responses.setdefault(code, payload)
    return document


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.api_version,
        default_response_class=ORJSONResponse,
        lifespan=combine_lifespans(lifespan, mcp_http_app.lifespan),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    redis = Redis.from_url(settings.redis_url)
    limiter = RedisRateLimiter(redis)
    app.state.quota_service = QuotaService(redis, settings)

    @app.middleware("http")
    async def security_middleware(request, call_next):
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = supplied_request_id if REQUEST_ID_PATTERN.fullmatch(supplied_request_id) else uuid4().hex
        request.state.correlation_id = request_id
        context_token = bind_context(correlation_id=request_id)
        started = time.monotonic()
        try:
            path_scope = "auth" if request.url.path.startswith(f"{settings.api_prefix}/auth") else "api"
            limit = settings.rate_limit_auth_requests if path_scope == "auth" else settings.rate_limit_requests
            authorization = request.headers.get("Authorization", "")
            client_host = request.client.host if request.client else "unknown"
            identity = sha256((authorization or client_host).encode("utf-8")).hexdigest()
            if path_scope == "auth" or not authorization:
                decision = await limiter.check(
                    scope=path_scope,
                    identity=identity,
                    limit=limit,
                    window_seconds=settings.rate_limit_window_seconds,
                )
            else:
                decision = RateLimitDecision(True, limit, 0, limit)
            if not decision.allowed:
                response = JSONResponse(
                    status_code=429,
                    content={
                        "detail": {
                            "code": "rate_limit_exceeded",
                            "message": "rate limit exceeded",
                            "request_id": request_id,
                        }
                    },
                    headers={
                        "Retry-After": str(decision.retry_after),
                        "RateLimit-Limit": str(decision.limit),
                        "RateLimit-Remaining": str(decision.remaining),
                        "RateLimit-Reset": str(decision.retry_after),
                    },
                )
            else:
                try:
                    response = await call_next(request)
                except Exception as exc:
                    log_event(
                        logger,
                        "http.error",
                        level=logging.ERROR,
                        method=request.method,
                        path=request.url.path,
                        error_type=type(exc).__name__,
                    )
                    metrics.observe(
                        _request_operation(request),
                        (time.monotonic() - started) * 1000,
                        success=False,
                    )
                    raise
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            metrics.observe(
                _request_operation(request),
                duration_ms,
                success=response.status_code < 500,
            )
            log_event(
                logger,
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; object-src 'none'; base-uri 'self'; "
                "frame-ancestors 'none'; form-action 'self'"
            )
            if settings.hsts_enabled:
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            quota_decision = getattr(request.state, "quota_decision", None)
            response.headers["X-RateLimit-Limit"] = str(
                getattr(quota_decision, "limit", decision.limit)
            )
            response.headers["RateLimit-Remaining"] = str(
                getattr(quota_decision, "remaining", decision.remaining)
            )
            response.headers["RateLimit-Reset"] = str(
                getattr(quota_decision, "retry_after", decision.retry_after)
            )
            if quota_decision is not None:
                response.headers["RateLimit-Policy"] = quota_decision.policy_version
            return response
        finally:
            reset_context(context_token)

    def _request_operation(request) -> str:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        return f"http.{request.method.lower()}.{normalize_operation(path)}"

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(alerts_router)
    app.include_router(agent_tokens_router)
    app.include_router(audit_router)
    app.include_router(briefings_router)
    app.include_router(clusters_router)
    app.include_router(devtools_router)
    app.include_router(feed_router)
    app.include_router(feedback_router)
    app.include_router(preferences_router)
    app.include_router(sources_router)
    app.include_router(users_router)
    app.include_router(usage_router)
    app.include_router(telemetry_router)
    app.mount(MCP_MOUNT_PATH, mcp_http_app)
    default_openapi = app.openapi

    def annotated_openapi() -> dict[str, Any]:
        return _annotate_openapi(default_openapi())

    app.openapi = annotated_openapi  # type: ignore[method-assign]
    return app


app = create_app()
