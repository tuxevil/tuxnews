# Production Runbook And Disaster Recovery

This runbook is the operating manual for the public Tuxnews deployment behind
the existing reverse proxy that terminates TLS. It assumes the operator can
follow the compose deployment in this repository and has the environment
variables documented below. No plaintext secrets appear anywhere in this
repository; every secret is injected at deploy time.

Related documents: [alerts](alerts.md), [backups](backups.md),
[quotas](quotas.md), [performance](performance.md),
[telemetry-retention](telemetry-retention.md).

## 1. Deployment checklist (run in staging before release)

Secrets required (never committed): `TUXNEWS_ACCESS_TOKEN_SECRET`,
`TUXNEWS_REFRESH_TOKEN_SECRET`, `TUXNEWS_OBSERVABILITY_HASH_SALT`,
`POSTGRES_PASSWORD`, any LLM provider keys.

1. `docker compose config --quiet` — compose file is valid.
2. Backend gates: `ruff check . && mypy app && pytest -q` — all green.
3. Frontend gates: `npm run build` — clean.
4. Dependency gates: `pip-audit --severity=critical` and
   `npm audit --audit-level=critical` — no critical findings.
5. Start stack: `docker compose up -d --build --wait` with production env.
6. Migrations ran: `docker compose logs api | grep -q "Application startup complete"`.
7. Health: `curl -f http://localhost/health/status` → `healthy`/`ready`;
   every dependency `ok`.
8. Smoke: login as admin, `GET /api/v1/feed` returns 200 with
   `RateLimit-Policy` header; `/mcp/health` returns 200.
9. Alerts: `GET /api/v1/admin/alerts` → `fired` empty.
10. Backup: run `scripts/backup.sh`; verify a fresh run directory with
    `manifest.json`.
11. Load gate (staging only): `scripts/load_test.py --assert-feed-p95-ms 200`.

If any step fails, stop and follow the rollback procedure below.

## 2. Migration and rollback

### Migrations

- `alembic upgrade head` runs automatically on API container start.
- Verify before release: `docker compose run --rm api alembic current` matches
  the expected revision on the staging database.

### Rollback

1. Deploy the previous image tag: `docker compose up -d --build api worker`
   (or `docker compose pull` + `up` with a pinned tag).
2. Migrations are forward-only by design; if the new schema is incompatible,
   restore the previous database from the latest backup (section 4) instead
   of attempting a downgrade.
3. Confirm health and smoke steps 7-9 of the checklist.
4. Keep the failed release's image tag for postmortem.

## 3. Rotation and revocation

- JWT key rotation: set the new secret in
  `TUXNEWS_ACCESS_TOKEN_SECRET`/`TUXNEWS_REFRESH_TOKEN_SECRET` while keeping
  `TUXNEWS_*_PREVIOUS_SECRET` + `*_PREVIOUS_KEY_ID` +
  `*_PREVIOUS_VALID_UNTIL` populated during the grace window
  (`TUXNEWS_TOKEN_KEY_GRACE_SECONDS`). After the grace window, remove the
  previous values in a second deploy. Never rotate both secrets at once.
- Agent tokens: revoke via the admin agent-token endpoint; revocation takes
  effect on the next MCP request (hash-checked per request).
- Credential/LLM provider keys: rotate the provider secret, then run the
  smoke checklist; the gateway falls back to the deterministic summary while
  the provider is unavailable, so rotation is non-disruptive.
- Quota salt: `TUXNEWS_OBSERVABILITY_HASH_SALT` change invalidates
  pseudonymized labels in logs; treat it as a telemetry re-key, not an
  emergency.

## 4. Restore and disaster recovery

Full procedure with measured RTO/RPO: see [backups.md](backups.md).

1. `docker compose --profile backup up -d backup`.
2. `docker compose exec -T backup sh /scripts/backup.sh` (scheduled cadence
   determines RPO).
3. To restore: stop app traffic, drop and recreate the target database, run
   `scripts/restore.sh` with `TUXNEWS_RESTORE_DATABASE_URL` pointing at the
   clean database. Restore measures RTO automatically.
4. Qdrant: set `TUXNEWS_RESTORE_QDRANT=true` to upload snapshots; if they are
   missing or fail, run `scripts/rebuild_qdrant.py` (reconstruction from
   PostgreSQL).
5. news-archive: extracted by `restore.sh`.
6. Verify the marker of your choice with
   `test_backup_restore.py -k roundtrip` against a representative dataset
   before declaring DR complete.

## 5. Post-deploy smoke and diagnostics

```bash
curl -f http://localhost/health/live
curl -f http://localhost/health/ready                # 200 only when ready
curl -f http://localhost/health/status               # per-dependency latency
curl -f -H "Authorization: Bearer $TOKEN" http://localhost/api/v1/feed
curl -f http://localhost/mcp/health
```

Diagnostics on failure: each API log line is JSON with `correlation_id`;
pass `X-Request-ID` to correlate one request across `http.error` entries.
Admin surfaces: `GET /api/v1/admin/health/metrics` (percentiles, queue depth),
`GET /api/v1/admin/alerts` (fired rules with recovery text),
`GET /api/v1/admin/usage-events/report` (LLM cost/latency),
`GET /api/v1/admin/audit-events` (redacted audit trail).

Alert rules in force: `core_dependency_down`, `reconstructible_dependency_down`,
`http_error_rate`, `http_p95_latency`, `worker_queue_backlog`,
`llm_fallback_rate`, `llm_cost_anomaly`.

## 6. Incident matrix

| Incident | Symptoms | Severity | Containment | Recovery | Owner |
| --- | --- | --- | --- | --- | --- |
| Feed errors/slow | `http_error_rate` or `http_p95_latency` firing; feed 5xx | high | Check `http.error` logs by request_id; block offending tenant via quota | Fix query/index or provider; rollback if recent deploy | backend |
| LLM provider down | `llm_fallback_rate` firing; briefings show fallback summaries | medium | Switch `TUXNEWS_LLM_DEFAULT_PROFILE` to local/eco; raise no alerts spam (cooldown) | Restore provider key/endpoint; verify usage report cost normalizes | ai |
| Redis down | `core_dependency_down` critical; readiness 503; rate limits fail open (local) | critical | Keep serving; rate-limit bypass is bounded by quotas being unavailable — consider maintenance mode | Restart Redis, verify `worker_queue_backlog` clears | platform |
| Qdrant down | `reconstructible_dependency_down`; feed still served from PostgreSQL | medium | None required (readiness stays ready) | Restart Qdrant or rebuild from PostgreSQL | platform |
| PostgreSQL down | `core_dependency_down`; everything 5xx | critical | Put up maintenance page; do not restart blindly | Restore from backup (section 4) or repair disk; verify RTO | platform |
| news-archive unavailable | `save_article_md`/MCP archive errors | medium | Defer archive writes; ingestion unaffected | Check archive volume/disk space; restore tarball | platform |
| Auth/refresh issues | login 401s, refresh reuse audit events | high | Suspend public registration in production; revoke affected session family | Rotate JWT keys if compromise suspected; verify token revocation path | security |
| Worker stuck | `worker_queue_backlog` firing; jobs in retrying | medium | Scale workers or restart ARQ | Restart worker, verify heartbeat age gauge | backend |
| Quota rejection storm | clients see `quota_exceeded` 429 | medium | Verify `TUXNEWS_QUOTA_*` values; check `quota.rejected` logs (redacted) | Raise limits deliberately, never by lowering fail-open in production | backend |

Escalation: warning → owning team's working window; high → respond within 1
hour; critical → respond immediately and treat until readiness is `ready`
again.
