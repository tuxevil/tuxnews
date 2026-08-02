# User Lifecycle

## Registration and Invitations

Public registration is disabled in the Compose defaults. A deployment may
enable it only for a non-production environment. Production accounts are
created by an administrator through `POST /api/v1/admin/invitations`.

The response contains a one-time invitation secret for the deployment's email
adapter. Only its SHA-256 hash is stored. `POST /api/v1/auth/invitations/accept`
consumes the secret atomically; unknown, expired, revoked, and already-used
secrets return the same error and reveal no invitation data.

The application does not send email itself. Deployments must deliver the
one-time `token` through an external mail adapter without logging it.

## Roles and Suspension

Only an active administrator with `users:manage` can list, invite, suspend,
reactivate, change roles, or delete users. The last active administrator and
the administrator's own account cannot be removed or disabled.

Suspension marks the account inactive, revokes refresh sessions and agent
tokens, and advances `tokens_revoked_at`. REST and MCP access tokens issued
before that point remain invalid even if the account is later reactivated.
Reactivation does not restore revoked sessions or tokens.

## Recovery and Email Changes

`POST /api/v1/auth/password-recovery` always returns `202` with the same body,
whether or not the email exists. The external mail adapter delivers the
one-time token; confirmation changes the Argon2id password and revokes all
credentials.

An authenticated user starts an email change with
`POST /api/v1/auth/email-change`, supplying the current password. The new
address is applied only after the one-time confirmation token is consumed.
Email collisions and invalid tokens do not disclose account state.

## Deletion and Retention

Deletion is an administrator-only hard delete for the user's operational
data: sessions, agent tokens, action tokens, sources, articles, clusters,
feedback, briefings, schedules, discovery/ingestion runs, usage events, and
archive exports. Archive files are removed only through confined archive
paths. Existing audit rows are retained, detached from the deleted user, and
contain actor/resource identifiers rather than credentials or raw tokens.

Queued jobs re-check account activity before doing work. The delete path
attempts bounded Qdrant payload cleanup; if Qdrant is unavailable, the
operation is logged as deferred and the deployment must remove the deleted
user's vector payloads or rebuild the collection during its normal cleanup
run. Database, Redis, Qdrant, and provider credentials continue to follow the
secret-rotation procedure in `docs/secret-rotation.md`.
