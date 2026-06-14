"""Shared precheck receipt schema — builder와 verifier 단일 출처 (RTM-7c.4e closure).

Canonical field sets, semantic validation, hash payload construction. Raw 입력값을
exception message에 포함하지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from composition.paper_fast_loop_artifacts import (
    PAPER_FAST_LOOP_ARTIFACT_NAMES,
    PAPER_FAST_LOOP_SQLITE_ARTIFACT_NAMES,
)
from decision.canonical_json import payload_sha256

PRECHECK_RECEIPT_SCHEMA_VERSION = 1

# 단일 출처(paper_fast_loop_artifacts)에서 파생 — precheck/revalidation/final-preflight과 동일 순서.
PRECHECK_RECEIPT_ARTIFACT_NAMES: tuple[str, ...] = PAPER_FAST_LOOP_ARTIFACT_NAMES

_RECEIPT_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "checked_at",
        "market",
        "symbol",
        "enabled",
        "machine_outcome",
        "inspection_outcome",
        "reasons",
        "fingerprints_before",
        "fingerprints_after",
        "activation_authorized",
        "runtime_activation_outcome",
        "explicit_operator_approval_required",
        "writers_stopped_manual_confirmation_required",
        "receipt_sha256",
    }
)

FINGERPRINT_FIELDS = frozenset(
    {
        "name",
        "present",
        "is_regular_file",
        "size",
        "sha256",
        "user_version",
        "sidecar_suffixes",
    }
)

ALLOWED_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
_SQLITE_ARTIFACT_NAMES = PAPER_FAST_LOOP_SQLITE_ARTIFACT_NAMES
_JSON_SNAPSHOT_NAME = "execution_inputs_snapshot"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# KRX 종목코드는 ASCII 숫자 6자리만 허용 — Unicode decimal digit(전각·아랍-인디크 등) 거부.
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_SUPPORTED_MARKET = "KR"


class PrecheckReceiptError(ValueError):
    """Receipt builder 입력 검증 실패 — ``reason_code``만 carry; raw 값/path 미포함."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def strict_bool(value: object) -> bool | None:
    if type(value) is bool:
        return value
    return None


def strict_int(value: object) -> int | None:
    if type(value) is int and not isinstance(value, bool):
        return value
    return None


def is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def validate_checked_at(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise PrecheckReceiptError("receipt_invalid_checked_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PrecheckReceiptError("receipt_invalid_checked_at") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise PrecheckReceiptError("receipt_invalid_checked_at")


def checked_at_valid(value: object) -> bool:
    if type(value) is not str or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.tzinfo.utcoffset(parsed) is not None


def validate_market(market: str) -> None:
    if type(market) is not str or market != _SUPPORTED_MARKET:
        raise PrecheckReceiptError("receipt_invalid_market")


def market_valid(value: object) -> bool:
    return type(value) is str and value == _SUPPORTED_MARKET


def validate_symbol(symbol: str) -> None:
    if type(symbol) is not str or not _SYMBOL_PATTERN.fullmatch(symbol):
        raise PrecheckReceiptError("receipt_invalid_symbol")


def symbol_valid(value: object) -> bool:
    return type(value) is str and bool(_SYMBOL_PATTERN.fullmatch(value))


def validate_reasons(reasons: tuple[str, ...]) -> None:
    for reason in reasons:
        if type(reason) is not str or not reason.strip():
            raise PrecheckReceiptError("receipt_invalid_reason")


def validate_outcome_values(*, machine_outcome: str, inspection_outcome: str) -> None:
    if machine_outcome not in ("pass", "no_go"):
        raise PrecheckReceiptError("receipt_invalid_outcome")
    if inspection_outcome not in ("ok", "no_go"):
        raise PrecheckReceiptError("receipt_invalid_outcome")


_PRECHECK_ARTIFACT_CHANGED_PREFIX = "precheck_artifact_changed:"


def validate_outcome_semantics(
    *, machine_outcome: str, inspection_outcome: str, reasons: tuple[str, ...]
) -> None:
    validate_outcome_values(machine_outcome=machine_outcome, inspection_outcome=inspection_outcome)
    if machine_outcome == "pass":
        if inspection_outcome != "ok" or reasons:
            raise PrecheckReceiptError("receipt_semantic_mismatch")
    if inspection_outcome == "no_go" and machine_outcome != "no_go":
        raise PrecheckReceiptError("receipt_semantic_mismatch")
    if machine_outcome == "no_go" and not reasons:
        raise PrecheckReceiptError("receipt_semantic_mismatch")


def outcome_semantics_valid(
    machine_outcome: str, inspection_outcome: str, reasons: list[str]
) -> bool:
    if machine_outcome == "pass":
        if inspection_outcome != "ok" or reasons:
            return False
    if inspection_outcome == "no_go" and machine_outcome != "no_go":
        return False
    if machine_outcome == "no_go" and not reasons:
        return False
    return True


def _comparison_payload_from_dict(fp: dict[str, Any]) -> dict[str, object]:
    """Verifier 경로 — parse된 fingerprint dict를 canonical 비교 payload로 정규화."""
    sidecars = fp["sidecar_suffixes"]
    return {
        "name": fp["name"],
        "present": fp["present"],
        "is_regular_file": fp["is_regular_file"],
        "size": fp["size"],
        "sha256": fp["sha256"],
        "user_version": fp["user_version"],
        "sidecar_suffixes": list(sidecars),
    }


def _changed_artifacts_from_payloads(
    before_payloads: list[dict[str, object]],
    after_payloads: list[dict[str, object]],
) -> set[str]:
    changed: set[str] = set()
    for before, after in zip(before_payloads, after_payloads):
        if before != after:
            changed.add(str(before["name"]))
    return changed


def _changed_artifacts_from_reasons(
    reasons: tuple[str, ...] | list[str],
) -> tuple[set[str], bool]:
    """``precheck_artifact_changed:<artifact>`` reason 집합 — unknown/duplicate면 invalid."""
    seen: set[str] = set()
    artifacts: set[str] = set()
    for reason in reasons:
        if not reason.startswith(_PRECHECK_ARTIFACT_CHANGED_PREFIX):
            continue
        artifact = reason[len(_PRECHECK_ARTIFACT_CHANGED_PREFIX) :]
        if artifact not in PRECHECK_RECEIPT_ARTIFACT_NAMES:
            return set(), True
        if artifact in seen:
            return set(), True
        seen.add(artifact)
        artifacts.add(artifact)
    return artifacts, False


def _validate_observation_fingerprint_semantics(
    *,
    machine_outcome: str,
    reasons: tuple[str, ...] | list[str],
    before_payloads: list[dict[str, object]],
    after_payloads: list[dict[str, object]],
) -> None:
    """PASS before/after equality + NO_GO drift↔changed-reason 일치 — builder·verifier 공통."""
    if machine_outcome == "pass" and before_payloads != after_payloads:
        raise PrecheckReceiptError("receipt_semantic_mismatch")
    changed_fp = _changed_artifacts_from_payloads(before_payloads, after_payloads)
    changed_reasons, invalid_reason = _changed_artifacts_from_reasons(reasons)
    if invalid_reason or changed_fp != changed_reasons:
        raise PrecheckReceiptError("receipt_semantic_mismatch")


def validate_observation_semantics(
    *,
    machine_outcome: str,
    inspection_outcome: str,
    reasons: tuple[str, ...],
    fingerprints_before: tuple[Any, ...],
    fingerprints_after: tuple[Any, ...],
) -> None:
    """Builder object 경로 — outcome + fingerprint observation semantics."""
    validate_outcome_semantics(
        machine_outcome=machine_outcome,
        inspection_outcome=inspection_outcome,
        reasons=reasons,
    )
    before_payloads = [fingerprint_to_receipt_payload(fp) for fp in fingerprints_before]
    after_payloads = [fingerprint_to_receipt_payload(fp) for fp in fingerprints_after]
    _validate_observation_fingerprint_semantics(
        machine_outcome=machine_outcome,
        reasons=reasons,
        before_payloads=before_payloads,
        after_payloads=after_payloads,
    )


def observation_semantics_valid(
    machine_outcome: str,
    inspection_outcome: str,
    reasons: list[str],
    fingerprints_before: list[dict[str, Any]],
    fingerprints_after: list[dict[str, Any]],
) -> bool:
    """Verifier dict 경로 — semantic mismatch면 False (raw payload 미포함)."""
    if not outcome_semantics_valid(machine_outcome, inspection_outcome, reasons):
        return False
    try:
        _validate_observation_fingerprint_semantics(
            machine_outcome=machine_outcome,
            reasons=reasons,
            before_payloads=[_comparison_payload_from_dict(fp) for fp in fingerprints_before],
            after_payloads=[_comparison_payload_from_dict(fp) for fp in fingerprints_after],
        )
    except PrecheckReceiptError:
        return False
    return True


def validate_fingerprint_sequence(
    fingerprints: tuple[Any, ...],
) -> None:
    if len(fingerprints) != len(PRECHECK_RECEIPT_ARTIFACT_NAMES):
        raise PrecheckReceiptError("receipt_invalid_fingerprint_count")
    names = [fp.name for fp in fingerprints]
    if len(set(names)) != len(names):
        raise PrecheckReceiptError("receipt_invalid_fingerprint_order")
    if any(name not in PRECHECK_RECEIPT_ARTIFACT_NAMES for name in names):
        raise PrecheckReceiptError("receipt_invalid_fingerprint_order")
    if tuple(names) != PRECHECK_RECEIPT_ARTIFACT_NAMES:
        raise PrecheckReceiptError("receipt_invalid_fingerprint_order")
    for fp in fingerprints:
        validate_artifact_fingerprint(fp)


def validate_receipt_fingerprints(
    fingerprints_before: tuple[Any, ...],
    fingerprints_after: tuple[Any, ...],
) -> None:
    validate_fingerprint_sequence(fingerprints_before)
    validate_fingerprint_sequence(fingerprints_after)


def _sidecar_suffixes_valid(sidecars: tuple[str, ...]) -> bool:
    """허용 suffix만, duplicate 없음, canonical generator 순서."""
    seen: set[str] = set()
    ordered: list[str] = []
    for suffix in sidecars:
        if type(suffix) is not str or suffix not in ALLOWED_SIDECAR_SUFFIXES:
            return False
        if suffix in seen:
            return False
        seen.add(suffix)
        ordered.append(suffix)
    canonical = [suffix for suffix in ALLOWED_SIDECAR_SUFFIXES if suffix in seen]
    return ordered == canonical


def validate_fingerprint_semantics(
    *,
    name: str,
    present: bool,
    is_regular_file: bool,
    size: int | None,
    sha256: str | None,
    user_version: int | None,
    sidecar_suffixes: tuple[str, ...],
) -> None:
    """Builder·verifier 공통 fingerprint 의미 규칙 — 실패 시 ``PrecheckReceiptError``."""
    if type(name) is not str:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")

    if not present:
        if is_regular_file:
            raise PrecheckReceiptError("receipt_invalid_fingerprint")
        if size is not None or sha256 is not None or user_version is not None:
            raise PrecheckReceiptError("receipt_invalid_fingerprint")
        if sidecar_suffixes:
            raise PrecheckReceiptError("receipt_invalid_fingerprint")
        return

    if not is_regular_file:
        if size is not None or sha256 is not None or user_version is not None:
            raise PrecheckReceiptError("receipt_invalid_fingerprint")
        if not _sidecar_suffixes_valid(sidecar_suffixes):
            raise PrecheckReceiptError("receipt_invalid_fingerprint")
        return

    if size is None or size < 0 or not is_hex64(sha256):
        raise PrecheckReceiptError("receipt_invalid_fingerprint")
    if user_version is not None and user_version < 0:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")
    if name == _JSON_SNAPSHOT_NAME and user_version is not None:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")
    if name not in PRECHECK_RECEIPT_ARTIFACT_NAMES:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")
    if not _sidecar_suffixes_valid(sidecar_suffixes):
        raise PrecheckReceiptError("receipt_invalid_fingerprint")


def _strict_fingerprint_fields_from_object(fp: Any) -> dict[str, Any]:
    """``ArtifactFingerprint`` → strict typed record. 실패 시 ``PrecheckReceiptError``."""
    present = strict_bool(fp.present)
    if present is None:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")
    is_regular_file = strict_bool(fp.is_regular_file)
    if is_regular_file is None:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")

    size: int | None = None
    if fp.size is not None:
        size = strict_int(fp.size)
        if size is None:
            raise PrecheckReceiptError("receipt_invalid_fingerprint")

    sha256: str | None = fp.sha256
    if sha256 is not None and type(sha256) is not str:
        raise PrecheckReceiptError("receipt_invalid_fingerprint")

    user_version: int | None = None
    if fp.user_version is not None:
        user_version = strict_int(fp.user_version)
        if user_version is None:
            raise PrecheckReceiptError("receipt_invalid_fingerprint")

    sidecars = fp.sidecar_suffixes
    if not isinstance(sidecars, tuple):
        raise PrecheckReceiptError("receipt_invalid_fingerprint")

    return {
        "name": fp.name,
        "present": present,
        "is_regular_file": is_regular_file,
        "size": size,
        "sha256": sha256,
        "user_version": user_version,
        "sidecar_suffixes": sidecars,
    }


def validate_artifact_fingerprint(fp: Any) -> None:
    """``ArtifactFingerprint`` semantic validation — builder 경로."""
    record = _strict_fingerprint_fields_from_object(fp)
    validate_fingerprint_semantics(**record)


def _validate_sidecar_suffixes(sidecars: tuple[str, ...]) -> None:
    if not _sidecar_suffixes_valid(sidecars):
        raise PrecheckReceiptError("receipt_invalid_fingerprint")


def fingerprint_to_receipt_payload(fp: Any) -> dict[str, object]:
    return {
        "name": fp.name,
        "present": fp.present,
        "is_regular_file": fp.is_regular_file,
        "size": fp.size,
        "sha256": fp.sha256,
        "user_version": fp.user_version,
        "sidecar_suffixes": list(fp.sidecar_suffixes),
    }


def precheck_activation_posture() -> dict[str, bool | str]:
    return {
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
    }


def build_receipt_hash_payload(
    *,
    schema_version: int,
    checked_at: str,
    market: str,
    symbol: str,
    enabled: bool,
    machine_outcome: str,
    inspection_outcome: str,
    reasons: tuple[str, ...],
    fingerprints_before: tuple[Any, ...],
    fingerprints_after: tuple[Any, ...],
) -> dict[str, object]:
    posture = precheck_activation_posture()
    return {
        "schema_version": schema_version,
        "checked_at": checked_at,
        "market": market,
        "symbol": symbol,
        "enabled": enabled,
        "machine_outcome": machine_outcome,
        "inspection_outcome": inspection_outcome,
        "reasons": list(reasons),
        "fingerprints_before": [fingerprint_to_receipt_payload(fp) for fp in fingerprints_before],
        "fingerprints_after": [fingerprint_to_receipt_payload(fp) for fp in fingerprints_after],
        **posture,
    }


def build_receipt_hash_payload_from_fingerprint_dicts(
    *,
    schema_version: int,
    checked_at: str,
    market: str,
    symbol: str,
    enabled: bool,
    machine_outcome: str,
    inspection_outcome: str,
    reasons: tuple[str, ...] | list[str],
    fingerprints_before: list[dict[str, Any]],
    fingerprints_after: list[dict[str, Any]],
) -> dict[str, object]:
    posture = precheck_activation_posture()
    return {
        "schema_version": schema_version,
        "checked_at": checked_at,
        "market": market,
        "symbol": symbol,
        "enabled": enabled,
        "machine_outcome": machine_outcome,
        "inspection_outcome": inspection_outcome,
        "reasons": list(reasons),
        "fingerprints_before": fingerprints_before,
        "fingerprints_after": fingerprints_after,
        **posture,
    }


def compute_receipt_sha256(hash_payload: dict[str, object]) -> str:
    return payload_sha256(hash_payload)


def activation_posture_valid(payload: dict[str, object]) -> bool:
    return (
        strict_bool(payload["activation_authorized"]) is False
        and payload["runtime_activation_outcome"] == "no_go"
        and strict_bool(payload["explicit_operator_approval_required"]) is True
        and strict_bool(payload["writers_stopped_manual_confirmation_required"]) is True
    )


def parse_sidecar_suffixes(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        if type(item) is not str or item not in ALLOWED_SIDECAR_SUFFIXES:
            return None
        if item in seen:
            return None
        seen.add(item)
        ordered.append(item)
    canonical = [suffix for suffix in ALLOWED_SIDECAR_SUFFIXES if suffix in seen]
    if ordered != canonical:
        return None
    return tuple(ordered)


def parse_fingerprint_dict(value: object) -> tuple[dict[str, Any], str | None]:
    if not isinstance(value, dict):
        return {}, "receipt_invalid_fingerprint"
    keys = set(value.keys())
    if keys - FINGERPRINT_FIELDS:
        return {}, "receipt_invalid_fingerprint"
    if FINGERPRINT_FIELDS - keys:
        return {}, "receipt_invalid_fingerprint"

    name = value["name"]
    if type(name) is not str:
        return {}, "receipt_invalid_fingerprint"

    present = strict_bool(value["present"])
    is_regular_file = strict_bool(value["is_regular_file"])
    if present is None or is_regular_file is None:
        return {}, "receipt_invalid_fingerprint"

    size_raw = value["size"]
    sha256_raw = value["sha256"]
    user_version_raw = value["user_version"]
    sidecars = parse_sidecar_suffixes(value["sidecar_suffixes"])
    if sidecars is None:
        return {}, "receipt_invalid_fingerprint"

    size: int | None
    sha256: str | None
    user_version: int | None

    if not present:
        if size_raw is not None or sha256_raw is not None or user_version_raw is not None:
            return {}, "receipt_invalid_fingerprint"
        if is_regular_file:
            return {}, "receipt_invalid_fingerprint"
        if sidecars:
            return {}, "receipt_invalid_fingerprint"
        size = None
        sha256 = None
        user_version = None
    elif not is_regular_file:
        if size_raw is not None or sha256_raw is not None or user_version_raw is not None:
            return {}, "receipt_invalid_fingerprint"
        size = None
        sha256 = None
        user_version = None
    else:
        size = strict_int(size_raw)
        if size is None or size < 0:
            return {}, "receipt_invalid_fingerprint"
        if type(sha256_raw) is not str or not is_hex64(sha256_raw):
            return {}, "receipt_invalid_fingerprint"
        sha256 = sha256_raw
        if user_version_raw is None:
            user_version = None
        else:
            user_version = strict_int(user_version_raw)
            if user_version is None or user_version < 0:
                return {}, "receipt_invalid_fingerprint"

    if name == _JSON_SNAPSHOT_NAME:
        if user_version is not None:
            return {}, "receipt_invalid_fingerprint"
    elif name not in _SQLITE_ARTIFACT_NAMES:
        return {}, "receipt_invalid_fingerprint"

    try:
        validate_fingerprint_semantics(
            name=name,
            present=present,
            is_regular_file=is_regular_file,
            size=size,
            sha256=sha256,
            user_version=user_version,
            sidecar_suffixes=sidecars,
        )
    except PrecheckReceiptError as exc:
        return {}, exc.reason_code

    return (
        {
            "name": name,
            "present": present,
            "is_regular_file": is_regular_file,
            "size": size,
            "sha256": sha256,
            "user_version": user_version,
            "sidecar_suffixes": list(sidecars),
        },
        None,
    )


def parse_fingerprint_list(value: object) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(value, list):
        return [], "receipt_invalid_fingerprint"
    if len(value) != len(PRECHECK_RECEIPT_ARTIFACT_NAMES):
        return [], "receipt_invalid_fingerprint_count"

    parsed: list[dict[str, Any]] = []
    names_seen: list[str] = []
    for item in value:
        fp, err = parse_fingerprint_dict(item)
        if err is not None:
            return [], err
        parsed.append(fp)
        names_seen.append(fp["name"])

    if names_seen != list(PRECHECK_RECEIPT_ARTIFACT_NAMES):
        return [], "receipt_invalid_fingerprint_order"

    return parsed, None


def receipt_top_level_fields() -> frozenset[str]:
    return _RECEIPT_TOP_LEVEL_FIELDS
