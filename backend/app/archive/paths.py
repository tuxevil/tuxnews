from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from slugify import slugify


class ArchivePathError(ValueError):
    """Raised when an archive path cannot be proven to stay within its root."""


def safe_slug(value: str, *, fallback: str = "article", max_length: int = 120) -> str:
    normalized = slugify(value, allow_unicode=False)[:max_length].strip("-._")
    return normalized or fallback


def _validate_segment(segment: str) -> str:
    if not segment or "\x00" in segment:
        raise ArchivePathError("invalid archive path segment")
    decoded = segment
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if decoded != segment or segment in {".", ".."} or "/" in segment or "\\" in segment:
        raise ArchivePathError("unsafe archive path segment")
    if Path(segment).is_absolute() or ":" in segment:
        raise ArchivePathError("absolute archive paths are not allowed")
    return segment


def confined_path(root: Path, *relative_segments: str) -> Path:
    root_path = root.expanduser().resolve(strict=False)
    safe_segments = [_validate_segment(segment) for segment in relative_segments]
    candidate = root_path.joinpath(*safe_segments)
    try:
        candidate.resolve(strict=False).relative_to(root_path)
    except ValueError as exc:
        raise ArchivePathError("archive path escapes configured root") from exc

    current = root_path
    for segment in safe_segments:
        current /= segment
        if current.is_symlink():
            raise ArchivePathError("symlink archive path components are not allowed")
    return candidate


def tenant_relative_path(tenant_id: int, relative_path: Path) -> Path:
    """Require persisted archive paths to stay under the owning tenant folder."""

    if tenant_id < 1:
        raise ArchivePathError("invalid archive tenant")
    expected_prefix = ("tenants", str(tenant_id))
    if relative_path.parts[:2] != expected_prefix:
        raise ArchivePathError("archive path belongs to another tenant")
    return relative_path


@dataclass(frozen=True)
class ArchiveWriteResult:
    relative_path: str
    checksum: str


class AtomicArchiveWriter:
    def __init__(self, root: Path) -> None:
        configured_root = root.expanduser()
        if configured_root.exists() and configured_root.is_symlink():
            raise ArchivePathError("archive root cannot be a symlink")
        self.root = configured_root.resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write_bytes(self, relative_path: Path, content: bytes) -> ArchiveWriteResult:
        safe_parts = relative_path.parts
        destination = confined_path(self.root, *safe_parts)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() and destination.is_symlink():
            raise ArchivePathError("archive destination cannot be a symlink")
        if destination.exists() and not destination.is_file():
            raise ArchivePathError("archive destination is not a regular file")

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=parent, prefix=".tmp-", delete=False) as temporary:
                temporary_name = temporary.name
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, destination)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise ArchivePathError("archive write failed") from exc
        checksum = hashlib.sha256(content).hexdigest()
        return ArchiveWriteResult(str(destination.relative_to(self.root)), checksum)

    def write_text(self, relative_path: Path, content: str) -> ArchiveWriteResult:
        return self.write_bytes(relative_path, content.encode("utf-8"))

    def read_text(self, relative_path: Path, *, max_bytes: int = 1_000_000) -> str:
        destination = confined_path(self.root, *relative_path.parts)
        if destination.is_symlink() or not destination.is_file():
            raise ArchivePathError("archive source is not a regular file")
        try:
            with destination.open("rb") as source:
                content = source.read(max_bytes + 1)
        except OSError as exc:
            raise ArchivePathError("archive read failed") from exc
        if len(content) > max_bytes:
            raise ArchivePathError("archive file is too large")
        return content.decode("utf-8")
