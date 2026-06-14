"""Runtime precheck receipt structural + hash verification (RTM-7c.4e).

Pure verifier over untrusted JSON-decoded receipt payloads. Validates strict schema,
semantic consistency, and ``receipt_sha256`` against a canonical payload recomputation.
Does **not** authenticate the author, prove freshness, or authorize runtime activation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from decision.canonical_json import payload_sha256

from composition.paper_fast_loop import (
    PRECHECK_RECEIPT_ARTIFACT_NAMES,
    PRECHECK_RECEIPT_SCHEMA_VERSION,
)

__all__ = [
    "ReceiptVerificationOutcome",
    "RuntimePrecheckReceiptVerification",
    "verify_runtime_precheck_receipt_payload",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_PATTERN = re.compile(r"^\d{6}$")
_ALLOWED_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
_SQLITE_ARTIFACT_NAMES = frozenset({"ledger", "trigger_journal", "active_decision_store"})
_JSON_SNAPSHOT_NAME = "execution_inputs_snapshot"

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

_FINGERPRINT_FIELDS = frozenset(
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


class ReceiptVerificationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class RuntimePrecheckReceiptVerification:
    """Receipt verification verdict — 원문 receipt/fingerprint payload 미보관."""

    outcome: ReceiptVerificationOutcome
    schema_version: int | None
    receipt_sha256: str | None
    reason_codes: tuple[str, ...]


def verify_runtime_precheck_receipt_payload(payload: object) -> RuntimePrecheckReceiptVerification:
    """Untrusted receipt JSON object의 strict schema·semantic·hash 일치를 검증한다.

    VALID는 schema 준수 + canonical hash 일치만 의미한다. 작성자 인증, approval,
    runtime authorization, freshness, writer-stop 증명이 아니다."""

    if not isinstance(payload, dict):
        return _invalid("receipt_not_object")

    keys = set(payload.keys())
    unknown = keys - _RECEIPT_TOP_LEVEL_FIELDS
    if unknown:
        return _invalid("receipt_unknown_field")
    missing = _RECEIPT_TOP_LEVEL_FIELDS - keys
    if missing:
        return _invalid("receipt_missing_field")

    schema_version = payload["schema_version"]
    if type(schema_version) is not int or isinstance(schema_version, bool):
        return _invalid("receipt_invalid_field")
    if schema_version != PRECHECK_RECEIPT_SCHEMA_VERSION:
        return _invalid("receipt_unsupported_schema")

    checked_at = payload["checked_at"]
    if not _valid_checked_at(checked_at):
        return _invalid("receipt_invalid_checked_at")

    market = payload["market"]
    if type(market) is not str or market != "KR":
        return _invalid("receipt_invalid_field")

    symbol = payload["symbol"]
    if type(symbol) is not str or not _SYMBOL_PATTERN.fullmatch(symbol):
        return _invalid("receipt_invalid_field")

    enabled = _strict_bool(payload["enabled"])
    if enabled is None:
        return _invalid("receipt_invalid_field")

    machine_outcome = payload["machine_outcome"]
    if machine_outcome not in ("pass", "no_go"):
        return _invalid("receipt_invalid_outcome")

    inspection_outcome = payload["inspection_outcome"]
    if inspection_outcome not in ("ok", "no_go"):
        return _invalid("receipt_invalid_outcome")

    reasons_raw = payload["reasons"]
    if not isinstance(reasons_raw, list):
        return _invalid("receipt_invalid_field")
    reasons: list[str] = []
    for item in reasons_raw:
        if type(item) is not str or not item.strip():
            return _invalid("receipt_invalid_field")
        reasons.append(item)

    if not _valid_activation_posture(payload):
        return _invalid("receipt_invalid_activation_posture")

    if not _valid_outcome_semantics(machine_outcome, inspection_outcome, reasons):
        return _invalid("receipt_semantic_mismatch")

    before, err = _parse_fingerprint_list(payload["fingerprints_before"])
    if err is not None:
        return _invalid(err)
    after, err = _parse_fingerprint_list(payload["fingerprints_after"])
    if err is not None:
        return _invalid(err)

    before_names = tuple(fp["name"] for fp in before)
    after_names = tuple(fp["name"] for fp in after)
    if before_names != after_names:
        return _invalid("receipt_fingerprint_identity_mismatch")

    stored_hash = payload["receipt_sha256"]
    if not _is_hex64(stored_hash):
        return _invalid("receipt_invalid_field")

    hash_payload = {
        "schema_version": schema_version,
        "checked_at": checked_at,
        "market": market,
        "symbol": symbol,
        "enabled": enabled,
        "machine_outcome": machine_outcome,
        "inspection_outcome": inspection_outcome,
        "reasons": reasons,
        "fingerprints_before": before,
        "fingerprints_after": after,
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
    }
    recomputed = payload_sha256(hash_payload)
    if recomputed != stored_hash:
        return _invalid("receipt_hash_mismatch", schema_version=schema_version, receipt_sha256=stored_hash)

    return RuntimePrecheckReceiptVerification(
        outcome=ReceiptVerificationOutcome.VALID,
        schema_version=schema_version,
        receipt_sha256=stored_hash,
        reason_codes=(),
    )


def _invalid(
    reason: str,
    *,
    schema_version: int | None = None,
    receipt_sha256: str | None = None,
) -> RuntimePrecheckReceiptVerification:
    return RuntimePrecheckReceiptVerification(
        outcome=ReceiptVerificationOutcome.INVALID,
        schema_version=schema_version,
        receipt_sha256=receipt_sha256,
        reason_codes=(reason,),
    )


def _strict_bool(value: object) -> bool | None:
    if type(value) is bool:
        return value
    return None


def _strict_int(value: object) -> int | None:
    if type(value) is int and not isinstance(value, bool):
        return value
    return None


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _valid_checked_at(value: object) -> bool:
    if type(value) is not str or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.tzinfo.utcoffset(parsed) is not None


def _valid_activation_posture(payload: dict[str, object]) -> bool:
    return (
        _strict_bool(payload["activation_authorized"]) is False
        and payload["runtime_activation_outcome"] == "no_go"
        and _strict_bool(payload["explicit_operator_approval_required"]) is True
        and _strict_bool(payload["writers_stopped_manual_confirmation_required"]) is True
    )


def _valid_outcome_semantics(
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


def _parse_fingerprint_list(value: object) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(value, list):
        return [], "receipt_invalid_fingerprint"
    if len(value) != len(PRECHECK_RECEIPT_ARTIFACT_NAMES):
        return [], "receipt_invalid_fingerprint_count"

    parsed: list[dict[str, Any]] = []
    names_seen: list[str] = []
    for item in value:
        fp, err = _parse_fingerprint(item)
        if err is not None:
            return [], err
        parsed.append(fp)
        names_seen.append(fp["name"])

    if names_seen != list(PRECHECK_RECEIPT_ARTIFACT_NAMES):
        if len(set(names_seen)) != len(names_seen) or any(
            n not in PRECHECK_RECEIPT_ARTIFACT_NAMES for n in names_seen
        ):
            return [], "receipt_invalid_fingerprint_order"
        return [], "receipt_invalid_fingerprint_order"

    return parsed, None


def _parse_fingerprint(value: object) -> tuple[dict[str, Any], str | None]:
    if not isinstance(value, dict):
        return {}, "receipt_invalid_fingerprint"
    keys = set(value.keys())
    if keys - _FINGERPRINT_FIELDS:
        return {}, "receipt_invalid_fingerprint"
    if _FINGERPRINT_FIELDS - keys:
        return {}, "receipt_invalid_fingerprint"

    name = value["name"]
    if type(name) is not str:
        return {}, "receipt_invalid_fingerprint"

    present = _strict_bool(value["present"])
    is_regular_file = _strict_bool(value["is_regular_file"])
    if present is None or is_regular_file is None:
        return {}, "receipt_invalid_fingerprint"

    size_raw = value["size"]
    sha256_raw = value["sha256"]
    user_version_raw = value["user_version"]
    sidecars_raw = value["sidecar_suffixes"]

    sidecars = _parse_sidecar_suffixes(sidecars_raw)
    if sidecars is None:
        return {}, "receipt_invalid_fingerprint"

    size: int | None
    sha256: str | None
    user_version: int | None

    if not present:
        if size_raw is not None or sha256_raw is not None or user_version_raw is not None:
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
        size = _strict_int(size_raw)
        if size is None or size < 0:
            return {}, "receipt_invalid_fingerprint"
        if not _is_hex64(sha256_raw):
            return {}, "receipt_invalid_fingerprint"
        sha256 = sha256_raw
        if user_version_raw is None:
            user_version = None
        else:
            user_version = _strict_int(user_version_raw)
            if user_version is None or user_version < 0:
                return {}, "receipt_invalid_fingerprint"

    if name == _JSON_SNAPSHOT_NAME:
        if user_version is not None:
            return {}, "receipt_invalid_fingerprint"
    elif name in _SQLITE_ARTIFACT_NAMES:
        pass  # user_version null or non-negative int — already validated
    else:
        return {}, "receipt_invalid_fingerprint"

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


def _parse_sidecar_suffixes(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        if type(item) is not str or item not in _ALLOWED_SIDECAR_SUFFIXES:
            return None
        if item in seen:
            return None
        seen.add(item)
        ordered.append(item)
    canonical = [suffix for suffix in _ALLOWED_SIDECAR_SUFFIXES if suffix in seen]
    if ordered != canonical:
        return None
    return tuple(ordered)
