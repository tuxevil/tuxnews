"""Ensure the runbook stays consistent with the shipped rule catalog and docs."""

from pathlib import Path

REPO = Path(__file__).parents[2]
RUNBOOK = REPO / "docs" / "runbook.md"


def test_runbook_incidents_reference_real_alert_rules() -> None:
    import sys

    sys.path.insert(0, str(REPO / "backend"))
    from app.observability.alerts import RULES

    rule_names = {rule.name for rule in RULES}
    text = RUNBOOK.read_text(encoding="utf-8")

    missing = [rule for rule in rule_names if rule not in text]
    assert not missing, f"runbook does not mention alert rules {missing}"


def test_runbook_references_existing_documents_and_scripts() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for path in (
        "docs/alerts.md",
        "docs/backups.md",
        "docs/quotas.md",
        "docs/performance.md",
        "docs/telemetry-retention.md",
        "scripts/backup.sh",
        "scripts/restore.sh",
        "scripts/rebuild_qdrant.py",
        "scripts/load_test.py",
    ):
        assert (REPO / path).is_file(), f"runbook references missing {path}"
        assert path.rsplit("/", 1)[-1] in text, f"runbook does not reference {path}"
