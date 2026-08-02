# Load, Performance And Dependency Baseline

Release gates are reproducible: `scripts/load_test.py` runs concurrent traffic
against a live deployment and enforces the feed p95 budget; the
`dependency-scan` CI job blocks critical vulnerabilities; the `load-gates` CI
job re-runs the same measurement on every push.

## How to run

```bash
docker compose up -d api                      # with quotas raised for measurement
TOKEN=$(login as admin)
TUXNEWS_LOAD_TOKEN="$TOKEN" python scripts/load_test.py \
  --base-url http://127.0.0.1:18000 --users 8 --duration 10 \
  --output benchmarks/feed-load-baseline.json --assert-feed-p95-ms 200
```

Exit code is non-zero when the gate fails. The script reports p50/p95/p99,
error rate (strict 2xx), and throughput per minute for feed, clusters,
sources, briefings and feedback. Ingestion is worker-side and covered by the
existing retry/backoff and isolation tests; archive and MCP have no REST
surface and are covered by contract tests.

## Release thresholds

| Gate | Threshold | Owner | Enforcement |
| --- | --- | --- | --- |
| Feed p95 | <= 200 ms | backend | `load_test.py --assert-feed-p95-ms 200` in CI `load-gates` |
| HTTP error rate | < 5% | backend | `http_error_rate` alert rule |
| HTTP p95 | <= 2000 ms | backend | `http_p95_latency` alert rule |
| Worker queue backlog | <= 100 | backend | `worker_queue_backlog` alert rule |
| LLM fallback rate | <= 30% | ai | `llm_fallback_rate` alert rule |
| Python critical vulns | 0 | security | `pip-audit --severity=critical` in CI `dependency-scan` |
| Frontend critical vulns | 0 | security | `npm audit --audit-level=critical` in CI `dependency-scan` |

## Baseline (2026-08-02, local Compose, 8 concurrent users)

`benchmarks/feed-load-baseline.json` (committed):

| Target | p95 ms | p99 ms | Errors | Throughput/min |
| --- | --- | --- | --- | --- |
| feed | 50.6 | 107.7 | 0 | 10 872 |
| clusters | 35.5 | 72.6 | 0 | 16 242 |
| sources | 36.1 | 94.4 | 0 | 15 672 |
| briefings | 36.8 | 81.8 | 0 | 15 504 |
| feedback | 40.0 | 117.3 | 0 | 15 042 |

The feed budget (200 ms) is met with more than 3x headroom. Baselines are
versioned; a regression in CI is visible as a failed gate or a committed
baseline change reviewed like any other change.

## Dependency scans

- Python: `pip-audit` against the installed backend (last run: no
  vulnerabilities).
- Frontend: `npm audit` (last run: 0 vulnerabilities).
- Container images: scan with your registry scanner (e.g. Trivy in GHCR)
  before release; the documented severity gate is CRITICAL blocks release,
  HIGH requires a documented exception in the runbook.
