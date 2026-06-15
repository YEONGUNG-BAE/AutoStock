"""Atomic create-new file publish/read for verified eligibility-artifact snapshots (RTM-7c.4x).

Pure filesystem API over the RTM-7c.4w canonical persistence payload. The writer encodes a
verified snapshot to canonical UTF-8 bytes and atomically publishes them to a caller-provided path
(create-new only — no overwrite). The reader performs read-only bounded I/O and re-validates bytes
through the existing persistence decoder exactly once.

This lane adds **explicit caller-provided path file I/O only**. It does **not** add a CLI,
automatic ``runtime/`` path selection, actual approval consumption, consumed marker, replay
prevention, signing/HMAC, Operator identity authentication, provenance verification, intent/evidence
lookup, TTL/freshness re-evaluation, or activation authorization. Runtime activation posture stays
NO-GO.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from composition.operator_approval_consumption_eligibility_artifact_persistence_payload import (
    ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES,
    EligibilityArtifactPersistencePayloadOutcome,
    EligibilityArtifactPersistencePayloadVerificationOutcome,
    decode_operator_approval_consumption_eligibility_artifact_payload,
    encode_verified_operator_approval_consumption_eligibility_artifact,
)
from composition.operator_approval_consumption_eligibility_artifact_verifier import (
    VerifiedOperatorApprovalConsumptionEligibilityArtifact,
)

__all__ = [
    "EligibilityArtifactFileReadOutcome",
    "EligibilityArtifactFileReadResult",
    "EligibilityArtifactFileWriteOutcome",
    "EligibilityArtifactFileWriteResult",
    "read_operator_approval_consumption_eligibility_artifact_file",
    "write_verified_operator_approval_consumption_eligibility_artifact_create_new",
]

_FILE_MODE = 0o600
_TEMP_PREFIX = ".tmp_eligibility_artifact_"

_REASON_INVALID_INPUT = "eligibility_artifact_file_invalid_input"
_REASON_INVALID_SNAPSHOT = "eligibility_artifact_file_invalid_snapshot"
_REASON_PARENT_MISSING = "eligibility_artifact_file_parent_missing"
_REASON_PARENT_NOT_DIRECTORY = "eligibility_artifact_file_parent_not_directory"
_REASON_DESTINATION_EXISTS = "eligibility_artifact_file_destination_exists"
_REASON_DESTINATION_NOT_REGULAR = "eligibility_artifact_file_destination_not_regular"
_REASON_TEMP_CREATE_FAILED = "eligibility_artifact_file_temp_create_failed"
_REASON_WRITE_FAILED = "eligibility_artifact_file_write_failed"
_REASON_PUBLISH_FAILED = "eligibility_artifact_file_publish_failed"
_REASON_SYNC_FAILED = "eligibility_artifact_file_sync_failed"
_REASON_MISSING = "eligibility_artifact_file_missing"
_REASON_NOT_REGULAR = "eligibility_artifact_file_not_regular"
_REASON_TOO_LARGE = "eligibility_artifact_file_too_large"
_REASON_READ_FAILED = "eligibility_artifact_file_read_failed"


class EligibilityArtifactFileWriteOutcome(StrEnum):
    WRITTEN = "written"
    NOT_WRITTEN = "not_written"
    INVALID = "invalid"


class EligibilityArtifactFileReadOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class EligibilityArtifactFileWriteResult:
    """Create-new file publish verdict — raw path/errno/exception 미보관."""

    outcome: EligibilityArtifactFileWriteOutcome
    reason_codes: tuple[str, ...]
    eligibility_artifact_sha256: str | None
    bytes_written: int | None


@dataclass(frozen=True)
class EligibilityArtifactFileReadResult:
    """Read-only file decode verdict — raw bytes/path/errno/exception 미보관."""

    outcome: EligibilityArtifactFileReadOutcome
    reason_codes: tuple[str, ...]
    snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact | None


def write_verified_operator_approval_consumption_eligibility_artifact_create_new(
    *,
    snapshot: object,
    destination: object,
) -> EligibilityArtifactFileWriteResult:
    """Verified snapshot을 canonical bytes로 인코딩한 뒤 destination에 create-new로 원자 publish한다.

    destination parent는 이미 존재해야 하며 writer는 directory를 생성하지 않는다. 기존 destination
    overwrite 금지. INVALID snapshot이면 filesystem 접근 0."""
    try:
        return _write(snapshot=snapshot, destination=destination)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _write_not_written(_REASON_WRITE_FAILED)


def read_operator_approval_consumption_eligibility_artifact_file(
    *,
    source: object,
) -> EligibilityArtifactFileReadResult:
    """Caller-provided path의 파일을 read-only로 bounded read하고 persistence decoder로 1회 검증한다.

    파일 생성/수정/삭제, sidecar/temp/reconcile 없음."""
    try:
        return _read(source=source)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _read_invalid(_REASON_READ_FAILED)


def _write(*, snapshot: object, destination: object) -> EligibilityArtifactFileWriteResult:
    encode_result = encode_verified_operator_approval_consumption_eligibility_artifact(snapshot)
    if encode_result.outcome is not EligibilityArtifactPersistencePayloadOutcome.CREATED:
        return EligibilityArtifactFileWriteResult(
            outcome=EligibilityArtifactFileWriteOutcome.INVALID,
            reason_codes=(_REASON_INVALID_SNAPSHOT,),
            eligibility_artifact_sha256=None,
            bytes_written=None,
        )

    payload_bytes = encode_result.payload_bytes
    assert payload_bytes is not None
    digest = encode_result.eligibility_artifact_sha256
    assert digest is not None

    path_error = _validate_exact_path(destination)
    if path_error is not None:
        return _write_not_written(path_error)

    dest = destination
    assert isinstance(dest, Path)

    parent_error = _validate_existing_parent_directory(dest.parent)
    if parent_error is not None:
        return _write_not_written(parent_error)

    dest_error = _validate_create_new_destination(dest)
    if dest_error is not None:
        return _write_not_written(dest_error)

    temp_path = dest.parent / f"{_TEMP_PREFIX}{secrets.token_hex(16)}"
    temp_fd: int | None = None
    published = False
    primary_reason: str | None = None

    try:
        temp_fd = _open_exclusive_temp(temp_path)
        if temp_fd is None:
            return _write_not_written(_REASON_TEMP_CREATE_FAILED)

        if not _write_all(temp_fd, payload_bytes):
            primary_reason = _REASON_WRITE_FAILED
            return _write_not_written(primary_reason)

        if not _fsync_fd(temp_fd):
            primary_reason = _REASON_SYNC_FAILED
            return _write_not_written(primary_reason)

        os.close(temp_fd)
        temp_fd = None

        try:
            os.link(temp_path, dest)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTDIR):
                primary_reason = _REASON_DESTINATION_EXISTS
            else:
                primary_reason = _REASON_PUBLISH_FAILED
            return _write_not_written(primary_reason)

        published = True

        try:
            os.unlink(temp_path)
        except OSError:
            # publish 성공 후 temp unlink 실패는 destination을 변경하지 않는다.
            pass

        if not _fsync_directory(dest.parent):
            primary_reason = _REASON_SYNC_FAILED
            return _write_not_written(primary_reason)

        return EligibilityArtifactFileWriteResult(
            outcome=EligibilityArtifactFileWriteOutcome.WRITTEN,
            reason_codes=(),
            eligibility_artifact_sha256=digest,
            bytes_written=len(payload_bytes),
        )
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if not published and temp_path.exists():
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _read(*, source: object) -> EligibilityArtifactFileReadResult:
    path_error = _validate_exact_path(source)
    if path_error is not None:
        return _read_invalid(_REASON_INVALID_INPUT)

    src = source
    assert isinstance(src, Path)

    try:
        st = os.lstat(src)
    except FileNotFoundError:
        return _read_invalid(_REASON_MISSING)
    except OSError:
        return _read_invalid(_REASON_READ_FAILED)

    if stat.S_ISLNK(st.st_mode):
        return _read_invalid(_REASON_NOT_REGULAR)
    if stat.S_ISDIR(st.st_mode):
        return _read_invalid(_REASON_NOT_REGULAR)
    if not stat.S_ISREG(st.st_mode):
        return _read_invalid(_REASON_NOT_REGULAR)

    if st.st_size > ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES:
        return _read_invalid(_REASON_TOO_LARGE)

    fd: int | None = None
    try:
        fd = _open_readonly_no_follow(src)
        if fd is None:
            return _read_invalid(_REASON_READ_FAILED)

        try:
            fst = os.fstat(fd)
        except OSError:
            return _read_invalid(_REASON_READ_FAILED)

        if not stat.S_ISREG(fst.st_mode):
            return _read_invalid(_REASON_NOT_REGULAR)

        if fst.st_size > ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES:
            return _read_invalid(_REASON_TOO_LARGE)

        payload_bytes = _read_exact(fd, fst.st_size)
        if payload_bytes is None:
            return _read_invalid(_REASON_READ_FAILED)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    decode_result = decode_operator_approval_consumption_eligibility_artifact_payload(
        payload_bytes
    )
    if (
        decode_result.outcome
        is EligibilityArtifactPersistencePayloadVerificationOutcome.VALID
    ):
        return EligibilityArtifactFileReadResult(
            outcome=EligibilityArtifactFileReadOutcome.VALID,
            reason_codes=(),
            snapshot=decode_result.snapshot,
        )

    return EligibilityArtifactFileReadResult(
        outcome=EligibilityArtifactFileReadOutcome.INVALID,
        reason_codes=(decode_result.reason_codes[0],),
        snapshot=None,
    )


def _validate_exact_path(value: object) -> str | None:
    if not isinstance(value, Path):
        return _REASON_INVALID_INPUT
    path_type = type(value)
    if path_type is Path:
        return None
    # macOS/Linux/Windows에서 ``Path(...)``가 반환하는 concrete 구현만 허용한다(사용자 subclass 거부).
    if path_type.__name__ in ("PosixPath", "WindowsPath") and issubclass(path_type, Path):
        return None
    return _REASON_INVALID_INPUT


def _validate_existing_parent_directory(parent: Path) -> str | None:
    try:
        st = os.lstat(parent)
    except FileNotFoundError:
        return _REASON_PARENT_MISSING
    except OSError:
        return _REASON_PARENT_MISSING

    if stat.S_ISLNK(st.st_mode):
        # parent symlink는 directory target이면 허용한다(lstat 기준 symlink 자체).
        if not parent.is_dir():
            return _REASON_PARENT_NOT_DIRECTORY
        return None

    if not stat.S_ISDIR(st.st_mode):
        return _REASON_PARENT_NOT_DIRECTORY
    return None


def _validate_create_new_destination(dest: Path) -> str | None:
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        return None
    except OSError:
        return _REASON_DESTINATION_EXISTS

    if stat.S_ISLNK(st.st_mode):
        return _REASON_DESTINATION_NOT_REGULAR
    if stat.S_ISDIR(st.st_mode):
        return _REASON_DESTINATION_NOT_REGULAR
    if stat.S_ISREG(st.st_mode):
        return _REASON_DESTINATION_EXISTS
    return _REASON_DESTINATION_NOT_REGULAR


def _open_flags(*, write: bool) -> int:
    flags = os.O_RDONLY if not write else os.O_WRONLY
    flags |= os.O_CREAT if write else 0
    flags |= os.O_EXCL if write else 0
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_exclusive_temp(path: Path) -> int | None:
    try:
        return os.open(path, _open_flags(write=True), _FILE_MODE)
    except OSError:
        return None


def _open_readonly_no_follow(path: Path) -> int | None:
    try:
        return os.open(path, _open_flags(write=False))
    except OSError:
        return None


def _write_all(fd: int, payload_bytes: bytes) -> bool:
    view = memoryview(payload_bytes)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(fd, view[offset:])
        except OSError:
            return False
        if written <= 0:
            return False
        offset += written
    return True


def _read_exact(fd: int, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        try:
            chunk = os.read(fd, remaining)
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    result = b"".join(chunks)
    if len(result) != size:
        return None
    return result


def _fsync_fd(fd: int) -> bool:
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False


def _fsync_directory(directory: Path) -> bool:
    dir_fd: int | None = None
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        os.fsync(dir_fd)
        return True
    except OSError:
        return False
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def _write_not_written(reason: str) -> EligibilityArtifactFileWriteResult:
    return EligibilityArtifactFileWriteResult(
        outcome=EligibilityArtifactFileWriteOutcome.NOT_WRITTEN,
        reason_codes=(reason,),
        eligibility_artifact_sha256=None,
        bytes_written=None,
    )


def _read_invalid(reason: str) -> EligibilityArtifactFileReadResult:
    return EligibilityArtifactFileReadResult(
        outcome=EligibilityArtifactFileReadOutcome.INVALID,
        reason_codes=(reason,),
        snapshot=None,
    )
