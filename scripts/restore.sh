#!/bin/sh
set -eu
set -o pipefail 2>/dev/null || true

# Restore the latest backup run into a clean target environment and measure RTO.
# All targets come from environment; never from the repository.

: "${TUXNEWS_BACKUP_DIR:=/backups}"
: "${TUXNEWS_RESTORE_DATABASE_URL:=postgresql://tuxnews:tuxnews@postgres:5432/tuxnews_restore_test}"
: "${TUXNEWS_ARCHIVE_ROOT:=/news-archive}"
: "${TUXNEWS_QDRANT_URL:=http://qdrant:6333}"
: "${TUXNEWS_RESTORE_QDRANT:=false}"

BACKUP_DIR="$TUXNEWS_BACKUP_DIR"
latest="$(find "$BACKUP_DIR" -maxdepth 1 -type d -name '2*' | sort | tail -1)"
if [ -z "$latest" ]; then
  echo "no backup runs found in $BACKUP_DIR" >&2
  exit 1
fi
echo "==> restoring from: $latest"
started="$(date +%s)"

# PostgreSQL: drop schema objects in the clean target, then load the dump.
gunzip -c "$latest/tuxnews-postgres.dump.gz" | pg_restore -d "$TUXNEWS_RESTORE_DATABASE_URL" --clean --if-exists --no-owner
echo "==> postgres restored"

# news-archive.
tar -xzf "$latest/tuxnews-archive.tar.gz" -C "$TUXNEWS_ARCHIVE_ROOT"
echo "==> archive restored"

# Qdrant: restore snapshots when requested; otherwise document reconstruction.
if [ "$TUXNEWS_RESTORE_QDRANT" = "true" ]; then
  if command -v python >/dev/null 2>&1; then
    python "$(dirname "$0")/qdrant_restore.py" "$latest" && echo "==> qdrant snapshots restored"
  else
    echo "==> qdrant restore requires python; run: python scripts/qdrant_restore.py '$latest'"
  fi
else
  echo "==> qdrant skipped (set TUXNEWS_RESTORE_QDRANT=true); rebuild path: python scripts/rebuild_qdrant.py"
fi

elapsed="$(($(date +%s) - started))"
echo "==> RTO measured: ${elapsed}s"
