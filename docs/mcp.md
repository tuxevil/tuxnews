# MCP Transports

The MCP contract is version `v1`. Existing tool names, resource URIs, scope
names, and required confirmation fields are stable; additive response fields
are backward-compatible, while renamed components or changed safety semantics
require a new contract version.

The application uses one `FastMCP` server for both remote and local clients.
Domain tools and resources are registered on that server; REST routes are not
converted into a second MCP implementation.

## SSE

The authenticated SSE endpoint is `/mcp/`. It is mounted inside the API and
accepts either the existing short-lived access JWT or a database-backed agent
token in the `Authorization` header. Agent tokens are checked on every MCP
request, so revocation and expiration take effect immediately for new
requests. `/mcp/health` is an unauthenticated operational health check.

The frontend nginx proxy forwards `/mcp/` to the API with HTTP/1.1, disables
buffering, and allows long-lived connections. The proxy must preserve the
`Authorization` header.

## Stdio

Local MCP clients can start the same server without opening a network port:

```bash
docker compose run --rm --no-deps api tuxnews-mcp
```

The installed `tuxnews-mcp` command uses Stdio and suppresses the FastMCP
startup banner so stdout remains reserved for MCP messages. Errors are
reported by FastMCP without exposing internal exception details to clients.

Agent-specific tokens, scopes, revocation, and human confirmation for
mutations are managed through `/api/v1/agent-tokens`:

- `POST /api/v1/agent-tokens` creates a token and returns its secret once.
- `GET /api/v1/agent-tokens` lists metadata without secrets.
- `POST /api/v1/agent-tokens/{id}/rotate` revokes the old token and returns a new secret once.
- `DELETE /api/v1/agent-tokens/{id}` revokes a token.

Supported agent scopes are `news:read`, `sources:write`, `feedback:write`,
and `archive:write`. Tools must enforce their required scope individually;
mutating tools should require explicit human confirmation and record the actor,
tenant, resource, outcome, and correlation ID in the audit event.
