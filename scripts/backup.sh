#!/bin/sh
set -eu
set -o pipefail 2>/dev/null || true

# PostgreSQL + Qdrant + archive backup with retention and an RPO manifest.
# No secrets live in the repo: all connection details come from environment.

: "${TUXNEWS_POSTGRES_URL:=postgresql://tuxnews:tuxnews@postgres:5432/tuxnews}"
: "${TUXNEWS_BACKUP_DIR:=/backups}"
: "${TUXNEWS_BACKUP_RETENTION_DAYS:=14}"
: "${TUXNEWS_ARCHIVE_ROOT:=/news-archive}"
: "${TUXNEWS_QDRANT_URL:=http://qdrant:6333}"

BACKUP_DIR="$TUXNEWS_BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$BACKUP_DIR/$stamp"
mkdir -p "$run_dir"

echo "==> backup run: $run_dir"

# PostgreSQL: compressed custom-format dump (restorable with pg_restore).
pg_dump "$TUXNEWS_POSTGRES_URL" -Fc -f "$run_dir/tuxnews-postgres.dump"
gzip -f "$run_dir/tuxnews-postgres.dump"
echo "==> postgres dump written"

# news-archive tree.
tar -czf "$run_dir/tuxnews-archive.tar.gz" -C "$TUXNEWS_ARCHIVE_ROOT" .
echo "==> archive tarball written"

# Qdrant snapshots per collection (prefix configurable via settings default).
if command -v curl >/dev/null 2>&1; then
  get_json() { curl -fsS "$1"; }
  post_json() { curl -fsS -X POST "$1"; }
  download() { curl -fsS "$1" -o "$2"; }
else
  get_json() { wget -qO- "$1"; }
  post_json() { wget -qO- --post-data="" "$1"; }
  download() { wget -q -O "$2" "$1"; }
fi
collections="$(get_json "$TUXNEWS_QDRANT_URL/collections" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')"
if [ -z "$collections" ]; then
  echo "==> qdrant: no collections found; reconstruction path applies (rebuild_qdrant.py)"
else
  for name in $collections; do
    snapshot="$(post_json "$TUXNEWS_QDRANT_URL/collections/$name/snapshots" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')"
    download "$TUXNEWS_QDRANT_URL/collections/$name/snapshots/$snapshot" "$run_dir/qdrant-$name.snapshot"
    echo "==> qdrant snapshot: $name"
  done
fi

# Manifest records RPO (dump age) so operators can verify the objective.
echo "{\"run\": \"$stamp\", \"created_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"collections\": \"$collections\"}" \
  > "$run_dir/manifest.json"
echo "==> manifest written"

# Retention: drop runs older than the configured window.
find "$BACKUP_DIR" -maxdepth 1 -type d -mtime +"$TUXNEWS_BACKUP_RETENTION_DAYS" -exec rm -rf {} +
echo "==> retention applied (older than ${TUXNEWS_BACKUP_RETENTION_DAYS}d removed)"
echo "==> backup complete"
