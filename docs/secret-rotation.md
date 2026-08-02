# Secret Rotation

JWT signing uses a small keyring in runtime configuration:

- `TUXNEWS_*_KEY_ID` and `TUXNEWS_*_SECRET` are the active signing key.
- `TUXNEWS_*_PREVIOUS_KEY_ID` and `TUXNEWS_*_PREVIOUS_SECRET` are accepted only
  until `TUXNEWS_*_PREVIOUS_VALID_UNTIL`.
- New access and refresh tokens include a `kid` header, while legacy tokens
  without `kid` remain compatible during the configured window.

## Normal Rotation

1. Generate a new secret outside the repository, for example with
   `openssl rand -hex 32`.
2. Set the new active secret and key ID. Move the current active key ID and
   secret into the matching `PREVIOUS_*` variables and set a UTC expiry window.
3. Restart API and worker instances with rolling replacement. New tokens use
   the new key; existing tokens continue to work until the expiry window.
4. After the window, remove all `PREVIOUS_*` variables and restart again.
5. Verify login, refresh, MCP SSE, Stdio startup, health endpoints, and worker
   connectivity. Never print the values while validating the deployment.

## Rollback and Compromise

Before the previous-key window ends, restore the old active key and keep the
new key as the previous key to roll back without invalidating either cohort.
If a key is compromised, omit it from the previous keyring, set a fresh active
key, and restart all JWT consumers immediately; this intentionally revokes
tokens signed by the compromised key.

Agent tokens are random opaque secrets stored only as hashes. Rotate or revoke
them through `/api/v1/agent-tokens`; their database state is checked on every
MCP request. Database, Redis, Qdrant, and LLM-provider credentials should be
rotated through the deployment secret manager, then applied with the same
rolling restart and health-check procedure. They must not be committed, baked
into images, or emitted in logs or Compose diagnostics.
