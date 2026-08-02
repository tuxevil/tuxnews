# Operational Alerts And Degradation

`GET /api/v1/admin/alerts` (admin scope `usage:read`) evaluates the current
process metrics and dependency health snapshot against the rule catalog in
`backend/app/observability/alerts.py`. Every rule declares a threshold, the
likely cause, the owning team, and a recovery action. The same evaluation is
covered by unit tests in `tests/test_alerts.py`, which run in CI.

## Rule catalog

| Rule | Severity | Threshold | Cause | Owner | Recovery |
| --- | --- | --- | --- | --- | --- |
| `core_dependency_down` | critical | any PostgreSQL/Redis probe down | DB or rate-limit store outage; networking/credentials | platform | Restart container, verify DNS/credentials, confirm readiness |
| `reconstructible_dependency_down` | warning | Qdrant or worker probe down | Vector index or worker outage | platform | Restart; reindex from PostgreSQL if the collection was lost |
| `http_error_rate` | warning | HTTP 5xx rate > 0.05 | Application/provider errors | backend | Inspect `http.error` logs by `request_id`, check providers, rollback |
| `http_p95_latency` | warning | HTTP p95 > 2000 ms | Slow queries, saturation, provider latency | backend | Review usage report, add indexes, scale workers |
| `worker_queue_backlog` | warning | ARQ queue depth > 100 | Saturated or stuck workers | backend | Scale workers, restart ARQ, inspect timeouts |
| `llm_fallback_rate` | warning | LLM fallback rate > 0.30 | Degraded provider | ai | Check provider status, switch profile, review usage report |
| `llm_cost_anomaly` | warning | daily estimated cost > threshold | Runaway jobs, expensive model | ai | Review per-tenant usage, pause jobs, adjust quotas |

## Noise control

Firing is deduplicated with a per-rule cooldown (`ALERT_COOLDOWN_SECONDS`, 300s):
a rule that stays over its threshold is reported once per cooldown window. The
endpoint returns `fired`, `resolved`, and the number of `suppressed` repeats so
operators can confirm the alert is not spamming.

## Degradation behavior

- PostgreSQL/Redis down → API readiness is `not_ready` (HTTP 503 on
  `/health/ready`) and the critical alert fires.
- Qdrant/worker down → API stays `ready` and `degraded`; the warning alert
  fires. Basic feed and archive paths only depend on PostgreSQL and keep
  working, which `test_basic_feed_survives_qdrant_and_worker_downtime` verifies.
- LLM provider down → briefing generation falls back to the deterministic
  summary and records a `fallback` usage event; the fallback-rate alert
  detects sustained degradation.

## Escalation

`warning` alerts are actionable within the owning team's working window.
`critical` alerts require immediate response: confirm the core store is
reachable, restore service, then verify readiness returns to `ready` before
treating the incident as closed.
