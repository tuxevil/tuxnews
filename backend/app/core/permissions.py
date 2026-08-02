from enum import StrEnum


class Scope(StrEnum):
    CONTENT_READ = "content:read"
    CONTENT_WRITE = "content:write"
    FEEDBACK_WRITE = "feedback:write"
    AGENT_TOKENS_READ = "agent_tokens:read"
    AGENT_TOKENS_WRITE = "agent_tokens:write"
    PREFERENCES_WRITE = "preferences:write"
    SOURCES_READ = "sources:read"
    SOURCES_WRITE = "sources:write"
    USERS_MANAGE = "users:manage"
    AUDIT_READ = "audit:read"
    USAGE_READ = "usage:read"


class AgentScope(StrEnum):
    NEWS_READ = "news:read"
    SOURCES_WRITE = "sources:write"
    FEEDBACK_WRITE = "feedback:write"
    ARCHIVE_WRITE = "archive:write"


ROLE_SCOPES: dict[str, frozenset[str]] = {
    "user": frozenset(
        {
            Scope.CONTENT_READ,
            Scope.CONTENT_WRITE,
            Scope.FEEDBACK_WRITE,
            Scope.AGENT_TOKENS_READ,
            Scope.AGENT_TOKENS_WRITE,
            Scope.PREFERENCES_WRITE,
            Scope.SOURCES_READ,
            Scope.SOURCES_WRITE,
        }
    ),
    "admin": frozenset({"*", *Scope}),
}


def scopes_for_role(role: str) -> frozenset[str]:
    return ROLE_SCOPES.get(role, frozenset())


def has_scope(scopes: frozenset[str], required: str) -> bool:
    return "*" in scopes or required in scopes
