# Telemetry Retention And Privacy

Every telemetry stream is classified with an explicit retention window and an
automation that enforces it. The table below is the source of truth; settings
live in `backend/app/core/config.py` and are overridable per deployment.

| Data type | Location | Retention | Automation |
| --- | --- | --- | --- |
| LLM usage events | `usage_events` (PostgreSQL, append-only) | `TUXNEWS_LLM_USAGE_RETENTION_DAYS` (default 90) | ARQ cron 03:00 `app.usage.service.purge_usage_events` |
| Audit events | `audit_events` (PostgreSQL) | `TUXNEWS_AUDIT_RETENTION_DAYS` (default 180) | ARQ cron 03:15 `app.audit.service.purge_audit_events` |
| Structured logs | stdout (container logs) | Not persisted by the application | Docker/collector retention applies; log volume is outside the app |
| Process metrics | In-memory registry | Lifecycle of the process | Reset on restart; no durable store |
| Error details | Inline in logs/audit | Same as the enclosing stream | Always redacted/truncated (see below) |

Retention functions are idempotent, time-bounded deletes exposed as reusable
services so an operator can run them on demand:

```bash
python -c "import asyncio; from app.audit.service import purge_expired_audit_events; print(asyncio.run(purge_expired_audit_events()))"
```

## Per-user deletion and export

`DELETE /api/v1/admin/telemetry/{user_id}` (admin scope `users:manage`):

- Deletes every `usage_events` row for the tenant (bypasses the append-only
  trigger through the documented maintenance override).
- Anonymizes `audit_events` for the tenant: identity fields (`user_id`,
  `tenant_id`, `actor_id`) are cleared, `actor_type` becomes `deleted`, and
  `details` are emptied, while action/resource/outcome/timestamp remain for
  diagnostics.

`GET /api/v1/admin/telemetry/{user_id}` returns bounded counts and the most
recent 200 rows of each stream with the same field selection used by the
existing audit export.

Deleting an account through the admin user lifecycle applies the same
anonymization to its audit history.

## Redaction guarantees

`backend/app/observability/logging.py` sanitizes every structured log record:

- Keys matching authorization, cookie, password, secret, token, credential,
  API key, prompt, content, body, or email are replaced with `[REDACTED]`.
- Tenant, actor, and user identifiers are pseudonymized with a keyed HMAC
  (`p_<16 hex>`), never stored raw.
- URLs with query strings have the query replaced by `[REDACTED]`; the path is
  kept for diagnostics.
- Strings are truncated to 256 characters and collections to 20 items.

`tests/test_telemetry_privacy.py` scans log output for JWT, Bearer-token,
e-mail, and private-key patterns and fails if any leak through, and verifies
that large error payloads never include full content.

## Administrative access and backups

- Telemetry export/delete endpoints require the `users:manage` scope (admin
  only); audit and usage reads require their own read scopes.
- Backups of PostgreSQL include telemetry; restoring a backup therefore
  restores historical usage/audit rows exactly as they were at backup time.
  Retention crons purge expired rows on the next schedule after restore.
- `quota` and `rate-limit` decisions are logged through the same redaction
  path, so rejection records never carry request bodies or tokens.
