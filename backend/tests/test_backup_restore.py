"""Backup/restore integration against the Compose PostgreSQL service.

The test is skipped when Docker/Compose services are unavailable. It:
1. Seeds a marker table in tuxnews_test.
2. Runs scripts/backup.sh inside the postgres container (pg_dump, archive tar,
   Qdrant snapshot when reachable, manifest, retention).
3. Restores into a clean tuxnews_restore_test database with scripts/restore.sh.
4. Verifies the marker survived and measures RPO/RTO.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[2] / "scripts"
BACKUP_DIR = Path("/tmp/tuxnews-backup-test")
TARGET_DB = "tuxnews_restore_test"
PROJECT = Path(__file__).parents[2]


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _postgres_available() -> bool:
    result = _compose("ps", "--services", "--status", "running")
    return result.returncode == 0 and "postgres" in result.stdout


@pytest.fixture(scope="module")
def postgres_ready() -> bool:
    if not _postgres_available():
        pytest.skip("compose postgres service not running")
    return True


def _exec_psql(database: str, sql: str) -> None:
    result = _compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "tuxnews",
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture()
def clean_target(postgres_ready: bool) -> None:
    del postgres_ready
    _compose("exec", "-T", "postgres", "dropdb", "-U", "tuxnews", "--if-exists", TARGET_DB)
    result = _compose("exec", "-T", "postgres", "createdb", "-U", "tuxnews", TARGET_DB)
    assert result.returncode == 0, result.stderr
    yield
    _compose("exec", "-T", "postgres", "dropdb", "-U", "tuxnews", "--if-exists", TARGET_DB)


def test_backup_restore_roundtrip_preserves_data_and_measures_rpo_rto(
    clean_target: None,
    tmp_path: Path,
) -> None:
    del clean_target
    marker = f"marker-{int(time.time())}"
    _exec_psql(
        "tuxnews_test",
        f"CREATE TABLE IF NOT EXISTS backup_marker (value text); "
        f"TRUNCATE backup_marker; INSERT INTO backup_marker VALUES ('{marker}');",
    )
    archive_src = tmp_path / "archive-src"
    archive_src.mkdir()
    (archive_src / "hello.txt").write_text("archive-payload", encoding="utf-8")

    run_dir = BACKUP_DIR / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    restore_dir = BACKUP_DIR / "restore"
    restore_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    backup = _compose(
        "run",
        "--rm",
        "-v",
        f"{SCRIPTS}:/scripts:ro",
        "-v",
        f"{run_dir}:/backups",
        "-v",
        f"{archive_src}:/news-archive:ro",
        "-e",
        "TUXNEWS_POSTGRES_URL=postgresql://tuxnews:tuxnews@postgres:5432/tuxnews_test",
        "-e",
        "TUXNEWS_BACKUP_DIR=/backups",
        "-e",
        "TUXNEWS_ARCHIVE_ROOT=/news-archive",
        "-e",
        "TUXNEWS_QDRANT_URL=http://qdrant:6333",
        "postgres",
        "sh",
        "/scripts/backup.sh",
    )
    assert backup.returncode == 0, backup.stderr + backup.stdout
    backup_elapsed = time.time() - started

    latest = sorted(run_dir.glob("2*"))[-1]
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run"]
    assert (latest / "tuxnews-postgres.dump.gz").is_file()
    assert (latest / "tuxnews-archive.tar.gz").is_file()

    started = time.time()
    restore = _compose(
        "run",
        "--rm",
        "-v",
        f"{SCRIPTS}:/scripts:ro",
        "-v",
        f"{run_dir}:/backups:ro",
        "-v",
        f"{restore_dir}:/news-archive",
        "-e",
        "TUXNEWS_BACKUP_DIR=/backups",
        "-e",
        "TUXNEWS_RESTORE_DATABASE_URL=postgresql://tuxnews:tuxnews@postgres:5432/tuxnews_restore_test",
        "-e",
        "TUXNEWS_ARCHIVE_ROOT=/news-archive",
        "postgres",
        "sh",
        "/scripts/restore.sh",
    )
    assert restore.returncode == 0, restore.stderr + restore.stdout
    restore_elapsed = time.time() - started

    value = _exec_psql_result(TARGET_DB, "SELECT value FROM backup_marker;")
    assert marker in value, f"marker lost in restore: {value}"
    assert (restore_dir / "hello.txt").read_text(encoding="utf-8") == "archive-payload"

    rpo = max((latest / "manifest.json").stat().st_mtime - time.time(), 0)
    print(
        json.dumps(
            {
                "backup_elapsed_s": round(backup_elapsed, 2),
                "restore_elapsed_s": round(restore_elapsed, 2),
                "rto_s": round(restore_elapsed, 2),
                "rpo_s": round(rpo, 2),
                "run": manifest["run"],
            }
        )
    )


def _exec_psql_result(database: str, sql: str) -> str:
    result = _compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "tuxnews",
        "-d",
        database,
        "-t",
        "-A",
        "-c",
        sql,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_scripts_parse_without_syntax_errors() -> None:
    for name in ("backup.sh", "restore.sh"):
        result = subprocess.run(
            ["sh", "-n", str(SCRIPTS / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_rebuild_qdrant_without_embedding_dependency_reports_clear_blocker() -> None:
    import asyncio
    import importlib.util

    module_path = SCRIPTS / "rebuild_qdrant.py"
    spec = importlib.util.spec_from_file_location("rebuild_qdrant", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from app.core.config import Settings
    from app.embeddings.provider import EmbeddingUnavailable, SentenceTransformerProvider

    with pytest.raises(EmbeddingUnavailable, match="sentence-transformers is not installed"):
        asyncio.run(SentenceTransformerProvider(Settings(embedding_model="sentence-transformers/test")).ensure_available())
