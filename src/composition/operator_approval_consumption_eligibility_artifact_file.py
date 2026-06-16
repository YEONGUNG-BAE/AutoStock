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
    validate_eligibility_artifact_persistence_payload_decode_result_invariants,
    validate_eligibility_artifact_persistence_payload_encode_result_invariants,
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
_CONCRETE_PATH_TYPE = type(Path())
_TEMP_UNLINK_ATTEMPTS = 2

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
_REASON_TEMP_CLOSE_FAILED = "eligibility_artifact_file_temp_close_failed"
_REASON_TEMP_CLEANUP_FAILED = "eligibility_artifact_file_temp_cleanup_failed"
_REASON_MISSING = "eligibility_artifact_file_missing"
_REASON_NOT_REGULAR = "eligibility_artifact_file_not_regular"
_REASON_TOO_LARGE = "eligibility_artifact_file_too_large"
_REASON_READ_FAILED = "eligibility_artifact_file_read_failed"


class EligibilityArtifactFileWriteOutcome(StrEnum):
    WRITTEN = "written"
    PUBLISHED_INCOMPLETE = "published_incomplete"
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
    return _write(snapshot=snapshot, destination=destination)


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
    path_error = _validate_exact_path(destination)
    if path_error is not None:
        return _write_invalid(path_error)

    dest = destination
    assert isinstance(dest, Path)

    try:
        encode_result = encode_verified_operator_approval_consumption_eligibility_artifact(snapshot)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _write_invalid(_REASON_INVALID_SNAPSHOT)

    try:
        encode_result_valid = validate_eligibility_artifact_persistence_payload_encode_result_invariants(
            encode_result
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        encode_result_valid = False

    if not encode_result_valid:
        return EligibilityArtifactFileWriteResult(
            outcome=EligibilityArtifactFileWriteOutcome.INVALID,
            reason_codes=(_REASON_INVALID_SNAPSHOT,),
            eligibility_artifact_sha256=None,
            bytes_written=None,
        )

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
    bytes_written = len(payload_bytes)

    parent_error = _validate_existing_parent_directory(dest.parent)
    if parent_error is not None:
        return _write_not_written(parent_error)

    dest_error = _validate_create_new_destination(dest)
    if dest_error is not None:
        return _write_not_written(dest_error)

    temp_path = dest.parent / f"{_TEMP_PREFIX}{secrets.token_hex(16)}"
    temp_fd: int | None = None
    temp_created = False
    temp_fd_open = False
    temp_close_attempted = False
    temp_close_complete = False
    temp_cleanup_attempted = False
    destination_published = False
    temp_cleanup_complete = False
    parent_sync_attempted = False
    parent_sync_confirmed = False
    primary_reasons: list[str] = []
    operation_stage = "temp_create"

    def _append_primary(reason: str) -> None:
        if reason not in primary_reasons:
            primary_reasons.append(reason)

    def _attempt_temp_close() -> None:
        nonlocal temp_fd, temp_fd_open, temp_close_attempted, temp_close_complete
        if not temp_fd_open or temp_fd is None or temp_close_complete:
            return
        temp_close_attempted = True
        try:
            os.close(temp_fd)
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            temp_close_complete = False
            return
        temp_fd = None
        temp_fd_open = False
        temp_close_complete = True

    def _attempt_temp_cleanup() -> None:
        nonlocal temp_cleanup_attempted, temp_cleanup_complete
        if not temp_created or temp_cleanup_complete:
            return
        temp_cleanup_attempted = True
        for _ in range(_TEMP_UNLINK_ATTEMPTS):
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                temp_cleanup_complete = True
                return
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                continue
            temp_cleanup_complete = True
            return
        temp_cleanup_complete = False

    def _attempt_parent_sync() -> None:
        nonlocal parent_sync_attempted, parent_sync_confirmed
        if not destination_published or parent_sync_confirmed:
            return
        parent_sync_attempted = True
        try:
            parent_sync_confirmed = _fsync_directory(dest.parent)
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            parent_sync_confirmed = False

    def _build_reason_codes() -> tuple[str, ...]:
        reasons: list[str] = []
        reasons.extend(primary_reasons)
        if temp_close_attempted and not temp_close_complete:
            reasons.append(_REASON_TEMP_CLOSE_FAILED)
        if temp_cleanup_attempted and not temp_cleanup_complete:
            reasons.append(_REASON_TEMP_CLEANUP_FAILED)
        if destination_published and parent_sync_attempted and not parent_sync_confirmed:
            reasons.append(_REASON_SYNC_FAILED)
        return _dedupe_reason_codes(reasons)

    def _fallback_reason_codes() -> tuple[str, ...]:
        reasons: list[str] = list(primary_reasons)
        if not reasons:
            reasons.append(_REASON_PUBLISH_FAILED if destination_published else _REASON_WRITE_FAILED)
        if temp_close_attempted and not temp_close_complete:
            reasons.append(_REASON_TEMP_CLOSE_FAILED)
        if temp_cleanup_attempted and not temp_cleanup_complete:
            reasons.append(_REASON_TEMP_CLEANUP_FAILED)
        if destination_published and parent_sync_attempted and not parent_sync_confirmed:
            reasons.append(_REASON_SYNC_FAILED)
        seen: set[str] = set()
        deduped: list[str] = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                deduped.append(reason)
        return tuple(deduped)

    def _finalize() -> EligibilityArtifactFileWriteResult:
        if destination_published:
            reason_codes = _build_reason_codes()
            if (
                temp_close_complete
                and temp_cleanup_complete
                and parent_sync_confirmed
                and not primary_reasons
            ):
                return EligibilityArtifactFileWriteResult(
                    outcome=EligibilityArtifactFileWriteOutcome.WRITTEN,
                    reason_codes=(),
                    eligibility_artifact_sha256=digest,
                    bytes_written=bytes_written,
                )
            return EligibilityArtifactFileWriteResult(
                outcome=EligibilityArtifactFileWriteOutcome.PUBLISHED_INCOMPLETE,
                reason_codes=reason_codes,
                eligibility_artifact_sha256=digest,
                bytes_written=bytes_written,
            )
        return EligibilityArtifactFileWriteResult(
            outcome=EligibilityArtifactFileWriteOutcome.NOT_WRITTEN,
            reason_codes=_build_reason_codes(),
            eligibility_artifact_sha256=None,
            bytes_written=None,
        )

    def _safe_finalize() -> EligibilityArtifactFileWriteResult:
        try:
            return _finalize()
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            if destination_published:
                _append_primary(_REASON_PUBLISH_FAILED)
                return EligibilityArtifactFileWriteResult(
                    outcome=EligibilityArtifactFileWriteOutcome.PUBLISHED_INCOMPLETE,
                    reason_codes=_fallback_reason_codes(),
                    eligibility_artifact_sha256=digest,
                    bytes_written=bytes_written,
                )
            if not primary_reasons:
                _append_primary(_REASON_WRITE_FAILED)
            return EligibilityArtifactFileWriteResult(
                outcome=EligibilityArtifactFileWriteOutcome.NOT_WRITTEN,
                reason_codes=_fallback_reason_codes(),
                eligibility_artifact_sha256=None,
                bytes_written=None,
            )

    def _recover_temp_create_side_effect() -> None:
        nonlocal temp_created
        if temp_created:
            return
        try:
            os.lstat(temp_path)
        except (FileNotFoundError, OSError):
            return
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return
        temp_created = True

    def _recover_publish_side_effect(temp_stat: os.stat_result | None) -> None:
        nonlocal destination_published
        if destination_published or temp_stat is None:
            return
        try:
            dest_stat = os.lstat(dest)
        except (FileNotFoundError, OSError):
            return
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return
        if (
            stat.S_ISREG(dest_stat.st_mode)
            and dest_stat.st_dev == temp_stat.st_dev
            and dest_stat.st_ino == temp_stat.st_ino
        ):
            destination_published = True

    def _cleanup_after_fatal(original: BaseException) -> None:
        try:
            _attempt_temp_close()
            _attempt_temp_cleanup()
            _attempt_parent_sync()
        except (MemoryError, KeyboardInterrupt, SystemExit):
            pass
        except Exception:
            pass
        raise original

    temp_stat: os.stat_result | None = None
    try:
        operation_stage = "temp_create"
        temp_fd = _open_exclusive_temp(temp_path)
        if temp_fd is None:
            _append_primary(_REASON_TEMP_CREATE_FAILED)
        else:
            temp_created = True
            temp_fd_open = True

        if temp_created and not primary_reasons:
            operation_stage = "write"
            if not _write_all(temp_fd, payload_bytes):
                _append_primary(_REASON_WRITE_FAILED)

        if temp_created and not primary_reasons:
            operation_stage = "file_fsync"
            if not _fsync_fd(temp_fd):
                _append_primary(_REASON_SYNC_FAILED)

        if temp_created and not primary_reasons:
            try:
                operation_stage = "fstat"
                temp_stat = os.fstat(temp_fd)
            except OSError:
                _append_primary(_REASON_PUBLISH_FAILED)

        if temp_created and not primary_reasons:
            try:
                operation_stage = "link"
                os.link(temp_path, dest)
            except OSError as exc:
                _recover_publish_side_effect(temp_stat)
                if exc.errno in (errno.EEXIST, errno.ENOTDIR):
                    _append_primary(_REASON_DESTINATION_EXISTS)
                else:
                    _append_primary(_REASON_PUBLISH_FAILED)
            except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                _recover_publish_side_effect(temp_stat)
                _cleanup_after_fatal(exc)
            else:
                destination_published = True

        if destination_published:
            try:
                operation_stage = "post_link_lstat"
                dest_stat = os.lstat(dest)
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                _append_primary(_REASON_PUBLISH_FAILED)
            else:
                if not stat.S_ISREG(dest_stat.st_mode):
                    _append_primary(_REASON_DESTINATION_NOT_REGULAR)
                elif (
                    temp_stat is None
                    or dest_stat.st_dev != temp_stat.st_dev
                    or dest_stat.st_ino != temp_stat.st_ino
                ):
                    _append_primary(_REASON_PUBLISH_FAILED)
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        if operation_stage == "temp_create":
            _recover_temp_create_side_effect()
        elif operation_stage == "link":
            _recover_publish_side_effect(temp_stat)
        _cleanup_after_fatal(exc)
    except Exception:
        if operation_stage == "temp_create":
            _recover_temp_create_side_effect()
            _append_primary(_REASON_TEMP_CREATE_FAILED)
        elif operation_stage == "link":
            _recover_publish_side_effect(temp_stat)
            _append_primary(_REASON_PUBLISH_FAILED)
        elif destination_published:
            _append_primary(_REASON_PUBLISH_FAILED)
        else:
            _append_primary(_REASON_WRITE_FAILED)

    _attempt_temp_close()
    _attempt_temp_cleanup()
    _attempt_parent_sync()
    return _safe_finalize()


def _read(*, source: object) -> EligibilityArtifactFileReadResult:
    path_error = _validate_exact_path(source)
    if path_error is not None:
        return _read_invalid(_REASON_INVALID_INPUT)

    src = source
    assert isinstance(src, Path)

    try:
        lst = os.lstat(src)
    except FileNotFoundError:
        return _read_invalid(_REASON_MISSING)
    except OSError:
        return _read_invalid(_REASON_READ_FAILED)

    if stat.S_ISLNK(lst.st_mode):
        return _read_invalid(_REASON_NOT_REGULAR)
    if stat.S_ISDIR(lst.st_mode):
        return _read_invalid(_REASON_NOT_REGULAR)
    if not stat.S_ISREG(lst.st_mode):
        return _read_invalid(_REASON_NOT_REGULAR)

    if lst.st_size > ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES:
        return _read_invalid(_REASON_TOO_LARGE)

    fd: int | None = None
    payload_bytes: bytes | None = None
    invalid_reason: str | None = None
    try:
        fd = _open_readonly_no_follow(src)
        if fd is None:
            invalid_reason = _REASON_READ_FAILED

        if invalid_reason is None:
            assert fd is not None
            try:
                fst_before = os.fstat(fd)
            except OSError:
                invalid_reason = _REASON_READ_FAILED

        if invalid_reason is None:
            assert fd is not None
            if (
                fst_before.st_dev != lst.st_dev
                or fst_before.st_ino != lst.st_ino
            ):
                invalid_reason = _REASON_READ_FAILED

        if invalid_reason is None and not stat.S_ISREG(fst_before.st_mode):
            invalid_reason = _REASON_NOT_REGULAR

        if (
            invalid_reason is None
            and fst_before.st_size > ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES
        ):
            invalid_reason = _REASON_TOO_LARGE

        if invalid_reason is None:
            assert fd is not None
            payload_bytes = _read_exact(fd, fst_before.st_size)
            if payload_bytes is None:
                invalid_reason = _REASON_READ_FAILED

        if invalid_reason is None:
            assert fd is not None
            try:
                extra = os.read(fd, 1)
            except OSError:
                invalid_reason = _REASON_READ_FAILED
            else:
                if extra != b"":
                    invalid_reason = _REASON_READ_FAILED

        if invalid_reason is None:
            assert fd is not None
            try:
                fst_after = os.fstat(fd)
            except OSError:
                invalid_reason = _REASON_READ_FAILED

        if invalid_reason is None:
            if (
                fst_after.st_dev != fst_before.st_dev
                or fst_after.st_ino != fst_before.st_ino
                or fst_after.st_size != fst_before.st_size
            ):
                invalid_reason = _REASON_READ_FAILED
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                invalid_reason = _REASON_READ_FAILED

    if invalid_reason is not None:
        return _read_invalid(invalid_reason)

    assert payload_bytes is not None

    try:
        decode_result = decode_operator_approval_consumption_eligibility_artifact_payload(
            payload_bytes
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise

    if not validate_eligibility_artifact_persistence_payload_decode_result_invariants(
        decode_result
    ):
        return _read_invalid(_REASON_READ_FAILED)

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
    if type(value) is not _CONCRETE_PATH_TYPE:
        return _REASON_INVALID_INPUT
    return None


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


def _dedupe_reason_codes(reasons: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return tuple(deduped)


def _fsync_fd(fd: int) -> bool:
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False


def _fsync_directory(directory: Path) -> bool:
    dir_fd: int | None = None
    fsync_complete = False
    close_complete = False
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        os.fsync(dir_fd)
        fsync_complete = True
    except OSError:
        return False
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                close_complete = False
            else:
                close_complete = True
    return fsync_complete and close_complete


def _write_not_written(reason: str) -> EligibilityArtifactFileWriteResult:
    return EligibilityArtifactFileWriteResult(
        outcome=EligibilityArtifactFileWriteOutcome.NOT_WRITTEN,
        reason_codes=(reason,),
        eligibility_artifact_sha256=None,
        bytes_written=None,
    )


def _write_invalid(reason: str) -> EligibilityArtifactFileWriteResult:
    return EligibilityArtifactFileWriteResult(
        outcome=EligibilityArtifactFileWriteOutcome.INVALID,
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
