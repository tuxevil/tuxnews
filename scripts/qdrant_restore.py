"""Restore Qdrant snapshots from a backup run via the HTTP upload API.

Usage:
    python scripts/qdrant_restore.py /backups/20260802T000000Z
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

from app.core.config import get_settings


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    snapshots = sorted(args.run_dir.glob("qdrant-*.snapshot"))
    if not snapshots:
        print(f"no qdrant snapshots found in {args.run_dir}")
        return 1
    async with httpx.AsyncClient(timeout=120) as client:
        for snapshot in snapshots:
            collection = snapshot.name.removeprefix("qdrant-").removesuffix(".snapshot")
            url = f"{settings.qdrant_url}/collections/{collection}/snapshots/upload?wait=true"
            with snapshot.open("rb") as handle:
                response = await client.put(url, files={"snapshot": (snapshot.name, handle)})
            if response.status_code >= 400:
                print(f"restore failed for {collection}: {response.status_code} {response.text[:200]}")
                return 1
            print(f"restored qdrant collection: {collection}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
