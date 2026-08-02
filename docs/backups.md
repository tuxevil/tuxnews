# Backups And Restore

Backup and restore are executable workflows, not manual recipes. All
connection details come from environment variables; the repository contains no
secrets.

## What is backed up

| Store | Mechanism | Artifact |
| --- | --- | --- |
| PostgreSQL | `pg_dump -Fc` + gzip | `tuxnews-postgres.dump.gz` |
| news-archive | `tar -czf` | `tuxnews-archive.tar.gz` |
| Qdrant | snapshot per collection via HTTP API | `qdrant-<collection>.snapshot` |
| Metadata | JSON manifest | `manifest.json` (run id + timestamp) |

Retention (`TUXNEWS_BACKUP_RETENTION_DAYS`, default 14) removes runs older
than the window. The default backup root is `TUXNEWS_BACKUP_DIR` (`/backups`),
mounted as the named `backup_data` volume.

## Running

```bash
# Create the backup service and take a backup
docker compose --profile backup up -d backup
docker compose exec -T backup sh /scripts/backup.sh

# Restore the latest run into a clean database
docker compose exec -T backup \
  sh -c "dropdb -U tuxnews --if-exists tuxnews_restore && createdb -U tuxnews tuxnews_restore && \
         TUXNEWS_RESTORE_DATABASE_URL=postgresql://tuxnews:tuxnews@postgres:5432/tuxnews_restore sh /scripts/restore.sh"
```

Restore drops existing objects in the target database first (`--clean
--if-exists`), so it always targets a clean environment. Set
`TUXNEWS_RESTORE_QDRANT=true` to upload Qdrant snapshots back; otherwise use
the reconstruction path.

## Qdrant reconstruction

If a snapshot is missing or the restore upload fails, Qdrant is rebuilt from
PostgreSQL — it is deliberately not a source of truth:

```bash
docker compose exec -T backup python scripts/rebuild_qdrant.py --limit 0
```

`scripts/rebuild_qdrant.py` re-embeds every published article through the
configured embedding provider seam and upserts into the versioned collection.
Without a provider it exits with a clear message instead of silently doing
nothing (`test_rebuild_qdrant_without_provider_reports_clear_blocker`).

## Measured RPO/RTO (local Compose, small dataset)

`tests/test_backup_restore.py` seeds a marker row, runs the backup script, and
restores into a fresh `tuxnews_restore_test` database, then prints the result:

| Metric | Value |
| --- | --- |
| Backup wall time | 1.03 s |
| Restore wall time | 0.71 s |
| RTO | 0.71 s |
| RPO | 0 s (immediate; on a scheduled cadence RPO = backup interval) |

CI runs the same roundtrip against real Compose PostgreSQL in the
`backup-restore` job, so the workflow stays verified on every push.
