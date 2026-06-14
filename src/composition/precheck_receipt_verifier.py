"""Runtime precheck receipt structural + hash verification (RTM-7c.4e).

Pure verifier over untrusted JSON-decoded receipt payloads. Validates strict schema,
semantic consistency, and ``receipt_sha256`` against a canonical payload recomputation.
Does **not** authenticate the author, prove freshness, or authorize runtime activation.

Duplicate object key detection is the CLI stdin JSON layer's responsibility — not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from composition.precheck_receipt_schema import (
    PRECHECK_RECEIPT_SCHEMA_VERSION,
    activation_posture_valid,
    build_receipt_hash_payload_from_fingerprint_dicts,
    checked_at_valid,
    compute_receipt_sha256,
    is_hex64,
    market_valid,
    observation_semantics_valid,
    parse_fingerprint_list,
    receipt_top_level_fields,
    strict_bool,
    symbol_valid,
)

__all__ = [
    "ReceiptVerificationOutcome",
    "RuntimePrecheckReceiptVerification",
    "verify_runtime_precheck_receipt_payload",
]


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

    top_level = receipt_top_level_fields()
    keys = set(payload.keys())
    if keys - top_level:
        return _invalid("receipt_unknown_field")
    if top_level - keys:
        return _invalid("receipt_missing_field")

    schema_version = payload["schema_version"]
    if type(schema_version) is not int or isinstance(schema_version, bool):
        return _invalid("receipt_invalid_field")
    if schema_version != PRECHECK_RECEIPT_SCHEMA_VERSION:
        return _invalid("receipt_unsupported_schema")

    if not checked_at_valid(payload["checked_at"]):
        return _invalid("receipt_invalid_checked_at")

    if not market_valid(payload["market"]):
        return _invalid("receipt_invalid_market")

    if not symbol_valid(payload["symbol"]):
        return _invalid("receipt_invalid_symbol")

    enabled = strict_bool(payload["enabled"])
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

    if not activation_posture_valid(payload):
        return _invalid("receipt_invalid_activation_posture")

    before, err = parse_fingerprint_list(payload["fingerprints_before"])
    if err is not None:
        return _invalid(err)
    after, err = parse_fingerprint_list(payload["fingerprints_after"])
    if err is not None:
        return _invalid(err)

    if not observation_semantics_valid(
        machine_outcome, inspection_outcome, reasons, before, after
    ):
        return _invalid("receipt_semantic_mismatch")

    stored_hash = payload["receipt_sha256"]
    if not is_hex64(stored_hash):
        return _invalid("receipt_invalid_field")

    hash_payload = build_receipt_hash_payload_from_fingerprint_dicts(
        schema_version=schema_version,
        checked_at=payload["checked_at"],
        market=payload["market"],
        symbol=payload["symbol"],
        enabled=enabled,
        machine_outcome=machine_outcome,
        inspection_outcome=inspection_outcome,
        reasons=reasons,
        fingerprints_before=before,
        fingerprints_after=after,
    )
    recomputed = compute_receipt_sha256(hash_payload)
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
