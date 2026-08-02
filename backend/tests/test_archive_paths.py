from pathlib import Path

import pytest
from app.archive.paths import ArchivePathError, AtomicArchiveWriter, confined_path, safe_slug, tenant_relative_path


def test_slug_is_ascii_bounded_and_has_fallback() -> None:
    assert safe_slug("  Café: A story! ") == "cafe-a-story"
    assert safe_slug("...", fallback="fallback") == "fallback"
    assert len(safe_slug("x" * 500)) <= 120


@pytest.mark.parametrize(
    "segment",
    ["../outside", "/tmp", "..", "%2e%2e", "nested/file", "nested\\file", "C:\\temp", ""],
)
def test_confined_path_rejects_traversal_segments(tmp_path: Path, segment: str) -> None:
    with pytest.raises(ArchivePathError):
        confined_path(tmp_path, segment)


def test_atomic_writer_confines_path_and_sets_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    writer = AtomicArchiveWriter(root)
    result = writer.write_text(Path("2026") / "article.md", "safe archive")

    destination = root / result.relative_path
    assert destination.read_text(encoding="utf-8") == "safe archive"
    assert oct(destination.stat().st_mode & 0o777) == "0o600"
    assert len(result.checksum) == 64


def test_tenant_archive_paths_cannot_cross_owner_prefix() -> None:
    assert tenant_relative_path(7, Path("tenants/7/standalone/article.md")) == Path(
        "tenants/7/standalone/article.md"
    )
    with pytest.raises(ArchivePathError):
        tenant_relative_path(7, Path("tenants/8/standalone/article.md"))


def test_symlink_components_are_rejected_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    writer = AtomicArchiveWriter(root)

    with pytest.raises(ArchivePathError):
        writer.write_text(Path("linked") / "secret.txt", "attack")
    assert target.read_text(encoding="utf-8") == "do not overwrite"
