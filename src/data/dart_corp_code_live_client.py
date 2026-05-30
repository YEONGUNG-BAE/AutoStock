from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

# 3C2: corp-code master ZIP immutable snapshot writer. HTTP/env/API key 읽기 없음.

_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class DartCorpCodeSnapshotError(RuntimeError):
    """corp-code master ZIP snapshot 기록 실패."""


def _is_zip_bytes(payload: bytes) -> bool:
    return payload.startswith(_ZIP_MAGIC_PREFIXES)


def ensure_zip_bytes(payload: bytes) -> None:
    """corp-code master 응답이 ZIP magic bytes인지 검증한다."""
    if not _is_zip_bytes(payload):
        raise DartCorpCodeSnapshotError("OpenDART corp-code endpoint did not return a ZIP")


def snapshot_filename_for_zip(*, zip_bytes: bytes, fetched_at: datetime) -> str:
    """immutable raw ZIP snapshot 파일명 (ZIP bytes SHA-8 + UTC timestamp)."""
    sha8 = hashlib.sha256(zip_bytes).hexdigest()[:8]
    compact = fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"raw_corp_code_{compact}_{sha8}.zip"


def write_corp_code_zip_snapshot(
    *,
    zip_bytes: bytes,
    snapshot_dir: Path,
    fetched_at: datetime,
) -> Path:
    """corp-code master ZIP bytes를 immutable snapshot path에 기록한다."""
    ensure_zip_bytes(zip_bytes)

    filename = snapshot_filename_for_zip(zip_bytes=zip_bytes, fetched_at=fetched_at)
    snapshot_path = snapshot_dir / filename
    if snapshot_path.exists():
        raise DartCorpCodeSnapshotError(f"snapshot already exists: {snapshot_path}")

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(zip_bytes)
    return snapshot_path
