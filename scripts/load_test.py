"""Concurrency load test against a running Tuxnews deployment.

Measures p50/p95/p99, error rate and throughput for feed, clusters, sources,
briefings and feedback reads, then optionally enforces a release gate.

Usage:
    TUXNEWS_LOAD_TOKEN=<admin token> python scripts/load_test.py \
        --base-url http://127.0.0.1:18000 --users 8 --duration 20 \
        --output benchmarks/feed-load-baseline.json --assert-feed-p95-ms 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from datetime import UTC, datetime
from typing import Any

import httpx

TARGETS = (
    ("feed", "GET", "/api/v1/feed", {"status": "published", "page_size": 20}),
    ("clusters", "GET", "/api/v1/clusters", {}),
    ("sources", "GET", "/api/v1/sources", {}),
    ("briefings", "GET", "/api/v1/briefings", {}),
    ("feedback", "GET", "/api/v1/feedback", {"article_id": 1}),
)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 2)


async def _worker(
    client: httpx.AsyncClient,
    token: str,
    target: tuple[str, str, str, dict[str, Any]],
    results: list[dict[str, Any]],
    stop: asyncio.Event,
) -> None:
    name, method, path, params = target
    headers = {"Authorization": f"Bearer {token}"}
    while not stop.is_set():
        started = time.perf_counter()
        try:
            response = await client.request(method, path, params=params, headers=headers)
            success = 200 <= response.status_code < 300
        except httpx.HTTPError:
            success = False
        duration_ms = (time.perf_counter() - started) * 1000
        results.append({"target": name, "duration_ms": duration_ms, "success": success})


async def _run_target(
    client: httpx.AsyncClient,
    token: str,
    target: tuple[str, str, str, dict[str, Any]],
    users: int,
    duration: float,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    stop = asyncio.Event()
    workers = [asyncio.create_task(_worker(client, token, target, results, stop)) for _ in range(users)]
    await asyncio.sleep(duration)
    stop.set()
    await asyncio.gather(*workers, return_exceptions=True)
    durations = [row["duration_ms"] for row in results]
    successes = sum(1 for row in results if row["success"])
    return {
        "target": target[0],
        "users": users,
        "duration_s": duration,
        "count": len(results),
        "throughput_per_minute": round(len(results) * 60 / duration, 2),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "max_ms": round(max(durations), 2) if durations else 0.0,
        "errors": len(results) - successes,
        "error_rate": round((len(results) - successes) / len(results), 4) if results else 0.0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("TUXNEWS_LOAD_BASE_URL", "http://127.0.0.1:18000"))
    parser.add_argument("--users", type=int, default=8)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--output", default=None)
    parser.add_argument("--assert-feed-p95-ms", type=float, default=None)
    args = parser.parse_args()

    token = os.getenv("TUXNEWS_LOAD_TOKEN", "")
    if not token:
        print("TUXNEWS_LOAD_TOKEN is required", file=__import__("sys").stderr)
        return 2

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "users": args.users,
        "duration_s": args.duration,
        "targets": [],
    }
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        for target in TARGETS:
            report["targets"].append(await _run_target(client, token, target, args.users, args.duration))

    feed = next(row for row in report["targets"] if row["target"] == "feed")
    print(json.dumps(report, indent=2))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"baseline written to {args.output}")

    if args.assert_feed_p95_ms is not None and feed["p95_ms"] > args.assert_feed_p95_ms:
        print(
            f"release gate failed: feed p95 {feed['p95_ms']}ms > {args.assert_feed_p95_ms}ms",
            file=__import__("sys").stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
