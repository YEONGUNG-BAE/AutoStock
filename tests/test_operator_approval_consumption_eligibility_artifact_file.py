"""RTM-7c.4x — Atomic create-new eligibility-artifact file I/O tests.

Explicit caller-provided ``tmp_path`` only. No CLI, no ``runtime/``, no consumption, no activation.
"""

from __future__ import annotations

import dataclasses
import errno
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibilityOutcome,
    assess_operator_approval_consumption_eligibility,
)
from composition.operator_approval_consumption_eligibility_artifact import (
    build_operator_approval_consumption_eligibility_artifact,
)
from composition.operator_approval_consumption_eligibility_artifact_file import (
    EligibilityArtifactFileReadOutcome,
    EligibilityArtifactFileWriteOutcome,
    read_operator_approval_consumption_eligibility_artifact_file as read_file,
    write_verified_operator_approval_consumption_eligibility_artifact_create_new as write_file,
)
from composition.operator_approval_consumption_eligibility_artifact_persistence_payload import (
    EligibilityArtifactPersistencePayloadOutcome,
    EligibilityArtifactPersistencePayloadResult,
    EligibilityArtifactPersistencePayloadVerification,
    EligibilityArtifactPersistencePayloadVerificationOutcome,
    encode_verified_operator_approval_consumption_eligibility_artifact as encode,
)
from composition.operator_approval_consumption_eligibility_artifact_verifier import (
    VerifiedOperatorApprovalConsumptionEligibilityArtifact,
    verify_and_snapshot_operator_approval_consumption_eligibility_artifact,
)
import composition.operator_approval_consumption_eligibility_artifact_file as file_mod
import composition.operator_approval_consumption_eligibility_artifact_persistence_payload as payload_mod

import test_operator_approval_consumption_eligibility as elig_helper


def _valid_snapshot() -> VerifiedOperatorApprovalConsumptionEligibilityArtifact:
    payload, ev, now = elig_helper._eligible_inputs()
    result = assess_operator_approval_consumption_eligibility(
        intent_payload=payload, evidence=ev, now=now
    )
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    art = build_operator_approval_consumption_eligibility_artifact(result).artifact
    assert art is not None
    snap = verify_and_snapshot_operator_approval_consumption_eligibility_artifact(
        dataclasses.asdict(art)
    ).snapshot
    assert snap is not None
    return snap


def _count_files(directory: Path) -> int:
    return sum(1 for _ in directory.iterdir())


# --- writer: normal ---


def test_write_normal_creates_file_with_mode_600(tmp_path: Path) -> None:
    snap = _valid_snapshot()
    dest = tmp_path / "artifact.json"
    enc = encode(snap)
    res = write_file(snapshot=snap, destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.WRITTEN
    assert res.reason_codes == ()
    assert res.bytes_written == len(enc.payload_bytes)
    assert res.eligibility_artifact_sha256 == snap.eligibility_artifact_sha256
    assert dest.exists()
    assert dest.read_bytes() == enc.payload_bytes
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_write_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    snap = _valid_snapshot()
    dest = Path("relative.json")
    res = write_file(snapshot=snap, destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.WRITTEN
    assert dest.read_bytes() == encode(snap).payload_bytes


# --- writer: invalid snapshot touches filesystem 0 ---


def test_write_invalid_snapshot_no_filesystem_access(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    before = _count_files(tmp_path)
    res = write_file(snapshot=object(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.INVALID
    assert res.reason_codes == ("eligibility_artifact_file_invalid_snapshot",)
    assert not dest.exists()
    assert _count_files(tmp_path) == before


# --- writer: path validation ---


@pytest.mark.parametrize("bad", [None, "path", object(), "/absolute/x.json"])
def test_write_invalid_destination_type(bad: object, tmp_path: Path) -> None:
    res = write_file(snapshot=_valid_snapshot(), destination=bad)  # type: ignore[arg-type]
    assert res.outcome is EligibilityArtifactFileWriteOutcome.NOT_WRITTEN
    assert res.reason_codes == ("eligibility_artifact_file_invalid_input",)


def test_write_path_subclass_rejected(tmp_path: Path) -> None:
    class _Sub(type(tmp_path)):
        pass

    res = write_file(snapshot=_valid_snapshot(), destination=_Sub(tmp_path / "x.json"))
    assert res.reason_codes == ("eligibility_artifact_file_invalid_input",)


def test_write_path_spoofed_posix_name_rejected(tmp_path: Path) -> None:
    class PosixPath(type(tmp_path)):  # noqa: N801 — deliberate name spoof
        pass

    res = write_file(snapshot=_valid_snapshot(), destination=PosixPath(tmp_path / "x.json"))
    assert res.reason_codes == ("eligibility_artifact_file_invalid_input",)


def test_write_path_custom_fspath_rejected(tmp_path: Path) -> None:
    class _FakePath(type(tmp_path)):
        def __fspath__(self) -> str:
            return str(tmp_path / "x.json")

    res = write_file(snapshot=_valid_snapshot(), destination=_FakePath(tmp_path / "x.json"))
    assert res.reason_codes == ("eligibility_artifact_file_invalid_input",)


def test_read_path_spoofed_posix_name_rejected(tmp_path: Path) -> None:
    class PosixPath(type(tmp_path)):  # noqa: N801
        pass

    res = read_file(source=PosixPath(tmp_path / "x.json"))
    assert res.reason_codes == ("eligibility_artifact_file_invalid_input",)


def test_write_parent_missing(tmp_path: Path) -> None:
    dest = tmp_path / "missing" / "artifact.json"
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_parent_missing",)


def test_write_parent_not_directory(tmp_path: Path) -> None:
    parent_file = tmp_path / "parent.txt"
    parent_file.write_text("x", encoding="utf-8")
    dest = parent_file / "artifact.json"
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_parent_not_directory",)


def test_write_destination_exists_regular_file(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    dest.write_bytes(b"existing")
    before = dest.read_bytes()
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_destination_exists",)
    assert dest.read_bytes() == before


def test_write_destination_is_directory(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    dest.mkdir()
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_destination_not_regular",)


def test_write_destination_symlink_to_file(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"x")
    link = tmp_path / "artifact.json"
    link.symlink_to(target)
    res = write_file(snapshot=_valid_snapshot(), destination=link)
    assert res.reason_codes == ("eligibility_artifact_file_destination_not_regular",)
    assert target.read_bytes() == b"x"


def test_write_destination_dangling_symlink(tmp_path: Path) -> None:
    link = tmp_path / "artifact.json"
    link.symlink_to(tmp_path / "missing.json")
    res = write_file(snapshot=_valid_snapshot(), destination=link)
    assert res.reason_codes == ("eligibility_artifact_file_destination_not_regular",)


def test_write_parent_symlink_allowed(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    link_parent = tmp_path / "link"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    dest = link_parent / "artifact.json"
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.WRITTEN
    assert dest.resolve().parent == real_parent.resolve()


# --- writer: failure cleanup ---


def test_write_short_write_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    calls = {"n": 0}

    def _short(fd: int, data: memoryview | bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 1
        raise OSError("injected")

    monkeypatch.setattr(file_mod.os, "write", _short)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_write_failed",)
    assert not dest.exists()
    assert _count_files(tmp_path) == 0


def test_write_second_write_failure_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    real_write = os.write
    calls = {"n": 0}

    def _fail_second(fd: int, data: memoryview | bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            chunk = bytes(data)[: max(1, len(data) // 2)]
            return real_write(fd, chunk)
        raise OSError("injected")

    monkeypatch.setattr(file_mod.os, "write", _fail_second)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_write_failed",)
    assert not dest.exists()


def test_write_fsync_failure_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    real_fsync = os.fsync
    calls = {"n": 0}

    def _fail_file_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected")
        real_fsync(fd)

    monkeypatch.setattr(file_mod.os, "fsync", _fail_file_fsync)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.NOT_WRITTEN
    assert res.reason_codes == ("eligibility_artifact_file_sync_failed",)
    assert not dest.exists()


def test_write_fsync_failure_plus_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    real_fsync = os.fsync
    real_unlink = os.unlink
    fsync_calls = {"n": 0}

    def _fail_file_fsync(fd: int) -> None:
        fsync_calls["n"] += 1
        if fsync_calls["n"] == 1:
            raise OSError("injected")
        real_fsync(fd)

    def _fail_temp_unlink(path: str | bytes) -> None:
        if str(path).startswith(str(tmp_path / file_mod._TEMP_PREFIX)):
            raise OSError("injected cleanup")
        real_unlink(path)

    monkeypatch.setattr(file_mod.os, "fsync", _fail_file_fsync)
    monkeypatch.setattr(file_mod.os, "unlink", _fail_temp_unlink)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.NOT_WRITTEN
    assert res.reason_codes == (
        "eligibility_artifact_file_sync_failed",
        "eligibility_artifact_file_temp_cleanup_failed",
    )
    assert not dest.exists()


def test_write_link_failure_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"

    def _fail_link(_src: str | bytes, _dst: str | bytes) -> None:
        raise OSError("injected")

    monkeypatch.setattr(file_mod.os, "link", _fail_link)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_publish_failed",)
    assert not dest.exists()
    assert _count_files(tmp_path) == 0


def test_write_parent_fsync_failure_after_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    snap = _valid_snapshot()

    def _fail_dir(_directory: Path) -> bool:
        return False

    monkeypatch.setattr(file_mod, "_fsync_directory", _fail_dir)
    res = write_file(snapshot=snap, destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.PUBLISHED_INCOMPLETE
    assert res.reason_codes == ("eligibility_artifact_file_sync_failed",)
    assert res.eligibility_artifact_sha256 == snap.eligibility_artifact_sha256
    assert res.bytes_written == len(encode(snap).payload_bytes)
    assert dest.exists()
    assert dest.read_bytes() == encode(snap).payload_bytes


def test_write_post_publish_unlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    snap = _valid_snapshot()
    real_unlink = os.unlink

    def _fail_temp_unlink(path: str | bytes) -> None:
        if str(path).startswith(str(tmp_path / file_mod._TEMP_PREFIX)):
            raise OSError("injected")
        real_unlink(path)

    monkeypatch.setattr(file_mod.os, "unlink", _fail_temp_unlink)
    res = write_file(snapshot=snap, destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.PUBLISHED_INCOMPLETE
    assert res.reason_codes == ("eligibility_artifact_file_temp_cleanup_failed",)
    assert dest.exists()
    assert dest.read_bytes() == encode(snap).payload_bytes
    assert any(p.name.startswith(file_mod._TEMP_PREFIX) for p in tmp_path.iterdir())


def test_write_post_publish_unlink_and_fsync_failure_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    snap = _valid_snapshot()
    real_unlink = os.unlink

    def _fail_temp_unlink(path: str | bytes) -> None:
        if str(path).startswith(str(tmp_path / file_mod._TEMP_PREFIX)):
            raise OSError("injected")
        real_unlink(path)

    monkeypatch.setattr(file_mod.os, "unlink", _fail_temp_unlink)
    monkeypatch.setattr(file_mod, "_fsync_directory", lambda _d: False)
    res = write_file(snapshot=snap, destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.PUBLISHED_INCOMPLETE
    assert res.reason_codes == (
        "eligibility_artifact_file_temp_cleanup_failed",
        "eligibility_artifact_file_sync_failed",
    )
    assert dest.exists()


def test_write_pre_publish_failure_plus_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    real_unlink = os.unlink

    def _fail_write(_fd: int, _data: memoryview | bytes) -> int:
        raise OSError("injected")

    def _fail_temp_unlink(path: str | bytes) -> None:
        if str(path).startswith(str(tmp_path / file_mod._TEMP_PREFIX)):
            raise OSError("injected cleanup")
        real_unlink(path)

    monkeypatch.setattr(file_mod.os, "write", _fail_write)
    monkeypatch.setattr(file_mod.os, "unlink", _fail_temp_unlink)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.NOT_WRITTEN
    assert res.reason_codes == (
        "eligibility_artifact_file_write_failed",
        "eligibility_artifact_file_temp_cleanup_failed",
    )
    assert not dest.exists()


def test_write_link_failure_plus_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    real_unlink = os.unlink

    def _fail_link(_src: str | bytes, _dst: str | bytes) -> None:
        raise OSError("injected")

    def _fail_temp_unlink(path: str | bytes) -> None:
        if str(path).startswith(str(tmp_path / file_mod._TEMP_PREFIX)):
            raise OSError("injected cleanup")
        real_unlink(path)

    monkeypatch.setattr(file_mod.os, "link", _fail_link)
    monkeypatch.setattr(file_mod.os, "unlink", _fail_temp_unlink)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.NOT_WRITTEN
    assert res.reason_codes == (
        "eligibility_artifact_file_publish_failed",
        "eligibility_artifact_file_temp_cleanup_failed",
    )
    assert not dest.exists()


def test_write_normal_no_temp_residue(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.WRITTEN
    assert not any(p.name.startswith(file_mod._TEMP_PREFIX) for p in tmp_path.iterdir())


def test_write_existing_destination_unchanged_on_publish_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"

    def _exist_on_link(_src: str | bytes, _dst: str | bytes) -> None:
        dest.write_bytes(b"raced")
        raise OSError(errno.EEXIST, "exists")

    monkeypatch.setattr(file_mod.os, "link", _exist_on_link)
    res = write_file(snapshot=_valid_snapshot(), destination=dest)
    assert res.reason_codes == ("eligibility_artifact_file_destination_exists",)
    assert dest.read_bytes() == b"raced"


# --- writer: fatal / sanitization / call counts ---


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_write_fatal_propagates(monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]) -> None:
    def _raise(_s: object) -> object:
        raise exc()

    monkeypatch.setattr(file_mod, "encode_verified_operator_approval_consumption_eligibility_artifact", _raise)
    with pytest.raises(exc):
        write_file(snapshot=_valid_snapshot(), destination=Path("x.json"))


def test_write_ordinary_exception_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(_s: object) -> object:
        raise ValueError("SECRET_/home/user/config.toml")

    monkeypatch.setattr(file_mod, "encode_verified_operator_approval_consumption_eligibility_artifact", _raise)
    res = write_file(snapshot=_valid_snapshot(), destination=tmp_path / "x.json")
    assert res.outcome is EligibilityArtifactFileWriteOutcome.NOT_WRITTEN
    blob = json.dumps([res.reason_codes, res.bytes_written])
    assert "SECRET_" not in blob and "/home/" not in blob


def test_write_call_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    real_encode = file_mod.encode_verified_operator_approval_consumption_eligibility_artifact

    def _enc(s: object) -> object:
        calls.append("encode")
        return real_encode(s)

    monkeypatch.setattr(file_mod, "encode_verified_operator_approval_consumption_eligibility_artifact", _enc)
    write_file(snapshot=_valid_snapshot(), destination=tmp_path / "x.json")
    assert calls == ["encode"]


# --- reader: normal ---


def test_read_normal_valid(tmp_path: Path) -> None:
    snap = _valid_snapshot()
    dest = tmp_path / "artifact.json"
    write_file(snapshot=snap, destination=dest)
    res = read_file(source=dest)
    assert res.outcome is EligibilityArtifactFileReadOutcome.VALID
    assert res.reason_codes == ()
    assert res.snapshot is not None
    assert dataclasses.asdict(res.snapshot) == dataclasses.asdict(snap)


# --- reader: path / missing / non-regular ---


@pytest.mark.parametrize("bad", [None, "path", object()])
def test_read_invalid_source_type(bad: object) -> None:
    res = read_file(source=bad)  # type: ignore[arg-type]
    assert res.reason_codes == ("eligibility_artifact_file_invalid_input",)


def test_read_missing(tmp_path: Path) -> None:
    res = read_file(source=tmp_path / "missing.json")
    assert res.reason_codes == ("eligibility_artifact_file_missing",)


def test_read_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    res = read_file(source=d)
    assert res.reason_codes == ("eligibility_artifact_file_not_regular",)


def test_read_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(encode(_valid_snapshot()).payload_bytes)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    res = read_file(source=link)
    assert res.reason_codes == ("eligibility_artifact_file_not_regular",)


def test_read_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    res = read_file(source=fifo)
    assert res.reason_codes == ("eligibility_artifact_file_not_regular",)


# --- reader: payload / noncanonical / size ---


def test_read_invalid_payload(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    dest.write_bytes(b"{}")
    res = read_file(source=dest)
    assert res.outcome is EligibilityArtifactFileReadOutcome.INVALID
    assert res.reason_codes == ("eligibility_artifact_missing_field",)


def test_read_noncanonical_payload(tmp_path: Path) -> None:
    canonical = encode(_valid_snapshot()).payload_bytes
    pretty = json.dumps(json.loads(canonical), indent=2).encode("utf-8")
    dest = tmp_path / "artifact.json"
    dest.write_bytes(pretty)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_persistence_payload_not_canonical",)


def test_read_over_limit(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    dest.write_bytes(b"x" * (file_mod.ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES + 1))
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_too_large",)


def test_read_size_changes_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    payload = encode(_valid_snapshot()).payload_bytes
    dest.write_bytes(payload)
    real_read = os.read

    def _short_read(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if data:
            return data[: max(1, len(data) // 2)]
        return data

    monkeypatch.setattr(file_mod.os, "read", _short_read)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)


def test_read_extra_trailing_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    payload = encode(_valid_snapshot()).payload_bytes
    dest.write_bytes(payload + b"x")
    real_fstat = os.fstat
    calls = {"n": 0}

    def _underreport_fstat(fd: int) -> os.stat_result:
        calls["n"] += 1
        st = real_fstat(fd)
        if calls["n"] == 1:
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                 len(payload), st.st_atime, st.st_mtime, st.st_ctime)
            )
        return st

    monkeypatch.setattr(file_mod.os, "fstat", _underreport_fstat)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)


def test_read_lstat_fstat_identity_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    dest.write_bytes(encode(_valid_snapshot()).payload_bytes)
    real_fstat = os.fstat

    def _mismatch_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result(
            (st.st_mode, st.st_ino + 1, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
             st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
        )

    monkeypatch.setattr(file_mod.os, "fstat", _mismatch_fstat)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)


def test_read_append_after_fstat_before_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    payload = encode(_valid_snapshot()).payload_bytes
    dest.write_bytes(payload)
    real_read = os.read
    calls = {"n": 0}

    def _append_on_second_read(fd: int, n: int) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            with open(dest, "ab") as fh:
                fh.write(b"x")
        return real_read(fd, n)

    monkeypatch.setattr(file_mod.os, "read", _append_on_second_read)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)


def test_read_fstat_size_change_after_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "artifact.json"
    payload = encode(_valid_snapshot()).payload_bytes
    dest.write_bytes(payload)
    real_fstat = os.fstat
    calls = {"n": 0}

    def _grow_after_read(fd: int) -> os.stat_result:
        calls["n"] += 1
        st = real_fstat(fd)
        if calls["n"] >= 2:
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                 st.st_size + 1, st.st_atime, st.st_mtime, st.st_ctime)
            )
        return st

    monkeypatch.setattr(file_mod.os, "fstat", _grow_after_read)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)


def test_read_decoder_not_called_on_unstable_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    payload = encode(_valid_snapshot()).payload_bytes
    dest.write_bytes(payload + b"x")
    real_fstat = os.fstat
    calls_decode: list[str] = []
    fstat_calls = {"n": 0}

    def _underreport_fstat(fd: int) -> os.stat_result:
        fstat_calls["n"] += 1
        st = real_fstat(fd)
        if fstat_calls["n"] == 1:
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                 len(payload), st.st_atime, st.st_mtime, st.st_ctime)
            )
        return st

    def _dec(_b: object) -> object:
        calls_decode.append("decode")
        return payload_mod.decode_operator_approval_consumption_eligibility_artifact_payload(_b)

    monkeypatch.setattr(file_mod.os, "fstat", _underreport_fstat)
    monkeypatch.setattr(
        file_mod,
        "decode_operator_approval_consumption_eligibility_artifact_payload",
        _dec,
    )
    read_file(source=dest)
    assert calls_decode == []


def test_read_decoder_called_once_on_stable_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    write_file(snapshot=_valid_snapshot(), destination=dest)
    calls: list[str] = []
    real_decode = file_mod.decode_operator_approval_consumption_eligibility_artifact_payload

    def _dec(b: object) -> object:
        calls.append("decode")
        return real_decode(b)

    monkeypatch.setattr(
        file_mod,
        "decode_operator_approval_consumption_eligibility_artifact_payload",
        _dec,
    )
    read_file(source=dest)
    assert calls == ["decode"]


def test_read_does_not_modify_source(tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    snap = _valid_snapshot()
    payload = encode(snap).payload_bytes
    dest.write_bytes(payload)
    before_bytes = dest.read_bytes()
    before_stat = dest.stat()
    read_file(source=dest)
    assert dest.read_bytes() == before_bytes
    assert dest.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert dest.stat().st_size == before_stat.st_size


# --- reader: fatal / sanitization / call counts ---


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_read_fatal_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: type[BaseException]) -> None:
    dest = tmp_path / "artifact.json"
    dest.write_bytes(encode(_valid_snapshot()).payload_bytes)

    def _raise(_s: object) -> object:
        raise exc()

    monkeypatch.setattr(file_mod, "decode_operator_approval_consumption_eligibility_artifact_payload", _raise)
    with pytest.raises(exc):
        read_file(source=dest)


def test_read_ordinary_exception_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    dest.write_bytes(encode(_valid_snapshot()).payload_bytes)

    def _raise(_b: object) -> object:
        raise RuntimeError("SECRET_/home/user/config.toml")

    monkeypatch.setattr(file_mod, "decode_operator_approval_consumption_eligibility_artifact_payload", _raise)
    res = read_file(source=dest)
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)
    assert "SECRET_" not in json.dumps(list(res.reason_codes))


def test_read_call_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dest = tmp_path / "artifact.json"
    write_file(snapshot=_valid_snapshot(), destination=dest)
    calls: list[str] = []
    real_decode = file_mod.decode_operator_approval_consumption_eligibility_artifact_payload

    def _dec(b: object) -> object:
        calls.append("decode")
        return real_decode(b)

    monkeypatch.setattr(file_mod, "decode_operator_approval_consumption_eligibility_artifact_payload", _dec)
    read_file(source=dest)
    assert calls == ["decode"]


# --- round-trip ---


def test_round_trip_13_fields_bytes_digest_mode(tmp_path: Path) -> None:
    snap = _valid_snapshot()
    dest = tmp_path / "artifact.json"
    enc = encode(snap)
    write_res = write_file(snapshot=snap, destination=dest)
    read_res = read_file(source=dest)
    assert write_res.outcome is EligibilityArtifactFileWriteOutcome.WRITTEN
    assert read_res.outcome is EligibilityArtifactFileReadOutcome.VALID
    assert read_res.snapshot is not None
    assert dataclasses.asdict(read_res.snapshot) == dataclasses.asdict(snap)
    assert dest.read_bytes() == enc.payload_bytes
    assert read_res.snapshot.eligibility_artifact_sha256 == snap.eligibility_artifact_sha256
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert dest.stat().st_size == len(enc.payload_bytes)


def test_second_write_same_destination_not_overwrite(tmp_path: Path) -> None:
    snap = _valid_snapshot()
    dest = tmp_path / "artifact.json"
    first = write_file(snapshot=snap, destination=dest)
    assert first.outcome is EligibilityArtifactFileWriteOutcome.WRITTEN
    before = dest.read_bytes()
    second = write_file(snapshot=snap, destination=dest)
    assert second.reason_codes == ("eligibility_artifact_file_destination_exists",)
    assert dest.read_bytes() == before


# --- import guard / --run exit 2 ---


def test_file_module_import_guard() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "composition" / "operator_approval_consumption_eligibility_artifact_file.py"
    text = path.read_text(encoding="utf-8")
    for forbidden in ("sqlite3", "requests", "httpx", "socket", "subprocess"):
        assert forbidden not in text


def test_run_exit_2_before_file_io() -> None:
    cli_path = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
    spec = importlib.util.spec_from_file_location("run_paper_fast_loop_file_gate", cli_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["--run"]) == 2


# --- dependency result invariants (encoder) ---


class _BadEncodeResult:
    """Property getter가 raise하는 malformed encoder result."""

    outcome = EligibilityArtifactPersistencePayloadOutcome.CREATED
    reason_codes = ()
    eligibility_artifact_sha256 = "abc"

    @property
    def payload_bytes(self) -> bytes:
        raise RuntimeError("SECRET_payload")


def _snap() -> VerifiedOperatorApprovalConsumptionEligibilityArtifact:
    return _valid_snapshot()


@pytest.mark.parametrize(
    "bad_result",
    [
        None,
        object(),
        {},
        SimpleNamespace(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=(),
            payload_bytes=b"x",
            eligibility_artifact_sha256="abc",
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=("eligibility_persistence_payload_invalid_snapshot",),
            payload_bytes=b"x",
            eligibility_artifact_sha256=_snap().eligibility_artifact_sha256,
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=(),
            payload_bytes=None,
            eligibility_artifact_sha256=_snap().eligibility_artifact_sha256,
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=(),
            payload_bytes=b"",
            eligibility_artifact_sha256=_snap().eligibility_artifact_sha256,
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=(),
            payload_bytes=b"x" * (payload_mod.ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES + 1),
            eligibility_artifact_sha256=_snap().eligibility_artifact_sha256,
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=(),
            payload_bytes=b"x",
            eligibility_artifact_sha256=None,
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
            reason_codes=(),
            payload_bytes=b"x",
            eligibility_artifact_sha256="NOT_HEX64",
        ),
        EligibilityArtifactPersistencePayloadResult(
            outcome=EligibilityArtifactPersistencePayloadOutcome.INVALID,
            reason_codes=("eligibility_persistence_payload_invalid_snapshot",),
            payload_bytes=b"x",
            eligibility_artifact_sha256="abc",
        ),
        _BadEncodeResult(),
    ],
)
def test_write_malformed_encoder_result_invalid_no_fs(
    bad_result: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    before = _count_files(tmp_path)

    def _bad(_s: object) -> object:
        return bad_result

    monkeypatch.setattr(
        file_mod,
        "encode_verified_operator_approval_consumption_eligibility_artifact",
        _bad,
    )
    res = write_file(snapshot=_snap(), destination=dest)
    assert res.outcome is EligibilityArtifactFileWriteOutcome.INVALID
    assert res.reason_codes == ("eligibility_artifact_file_invalid_snapshot",)
    assert not dest.exists()
    assert _count_files(tmp_path) == before


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_write_malformed_encoder_fatal_propagates(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raise(_s: object) -> object:
        raise exc()

    monkeypatch.setattr(
        file_mod,
        "encode_verified_operator_approval_consumption_eligibility_artifact",
        _raise,
    )
    with pytest.raises(exc):
        write_file(snapshot=_snap(), destination=Path("x.json"))


# --- dependency result invariants (decoder) ---


class _BadDecodeResult:
    outcome = EligibilityArtifactPersistencePayloadVerificationOutcome.VALID
    reason_codes = ()

    @property
    def snapshot(self) -> object:
        raise RuntimeError("SECRET_snapshot")


@pytest.mark.parametrize(
    "bad_result",
    [
        None,
        object(),
        {},
        SimpleNamespace(
            outcome=EligibilityArtifactPersistencePayloadVerificationOutcome.VALID,
            reason_codes=(),
            snapshot=_snap(),
        ),
        EligibilityArtifactPersistencePayloadVerification(
            outcome=EligibilityArtifactPersistencePayloadVerificationOutcome.VALID,
            reason_codes=("eligibility_persistence_payload_not_bytes",),
            snapshot=_snap(),
        ),
        EligibilityArtifactPersistencePayloadVerification(
            outcome=EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID,
            reason_codes=("eligibility_persistence_payload_not_bytes",),
            snapshot=_snap(),
        ),
        EligibilityArtifactPersistencePayloadVerification(
            outcome=EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID,
            reason_codes=(),
            snapshot=None,
        ),
        _BadDecodeResult(),
    ],
)
def test_read_malformed_decoder_result_read_failed(
    bad_result: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "artifact.json"
    write_file(snapshot=_snap(), destination=dest)

    def _bad(_b: object) -> object:
        return bad_result

    monkeypatch.setattr(
        file_mod,
        "decode_operator_approval_consumption_eligibility_artifact_payload",
        _bad,
    )
    res = read_file(source=dest)
    assert res.outcome is EligibilityArtifactFileReadOutcome.INVALID
    assert res.reason_codes == ("eligibility_artifact_file_read_failed",)
    assert res.snapshot is None
