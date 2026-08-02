# Quotas And Rate Limits

Authenticated REST, MCP HTTP/Stdio, and ARQ jobs share the Redis-backed quota
service in `backend/app/core/quota.py`. Quota keys are derived from the verified
tenant, scope, operation, and optional provider. Bearer token strings and client
secrets are never used as quota identity.

The default `quota-v1` policy checks:

- Tenant request volume per rolling fixed window.
- Scope and operation volume per window.
- Provider volume per window when a provider is involved.
- Optional daily estimated cost in cents.

Reservations are made by one Redis Lua script across all applicable dimensions.
Successful work commits the lease; exceptions and terminal worker failures
release it. Leases expire after `TUXNEWS_QUOTA_RESERVATION_TTL_SECONDS` as a
recovery boundary.

REST quota failures return HTTP `429` with a stable `quota_exceeded` code,
`Retry-After`, `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset`
headers. MCP returns the same code and retry value in a masked tool error.
Workers return a typed `quota_exceeded` result so ARQ callers can defer or
report the job without exposing Redis details.

Local development fails open when Redis is unavailable to preserve the existing
offline workflow. Production validation rejects `TUXNEWS_QUOTA_FAIL_OPEN=true`;
production deployments must configure Redis and an explicit quota policy.
