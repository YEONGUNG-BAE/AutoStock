"""Immutable verified precheck receipt snapshot (RTM-7c.4j).

Freezes a verifier-``VALID`` precheck receipt payload into a single immutable observation so
that downstream stages (4g byte-state revalidation, 4i receipt time observation) read the
*same* receipt instead of re-verifying and re-reading the raw mutable ``dict`` independently.
A snapshot is built **once** from the untrusted payload; every retained field is copied to an
immutable value (frozen ``ArtifactFingerprint`` tuples, an aware ``datetime``, ``str``/``bool``
scalars). No reference to the raw receipt object, no raw absolute/config path, and no
credential/env data is retained.

This module reuses the existing ``verify_runtime_precheck_receipt_payload`` and the shared
schema parse helpers — it builds **no** new canonical verifier, hash, or JSON parser. A
snapshot is an *observation*, not authenticity, not a signature, not Operator approval, not a
freshness/TTL verdict, and not activation authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from composition.precheck_receipt_schema import parse_fingerprint_list, strict_bool
from composition.precheck_receipt_verifier import (
    ReceiptVerificationOutcome,
    verify_runtime_precheck_receipt_payload,
)
from composition.sqlite_inspector import ArtifactFingerprint

__all__ = [
    "VerifiedReceiptSnapshotOutcome",
    "VerifiedPrecheckReceipt",
    "VerifiedPrecheckReceiptResult",
    "verify_and_snapshot_precheck_receipt",
]


class VerifiedReceiptSnapshotOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class VerifiedPrecheckReceipt:
    """Fully immutable verified receipt observation — raw payload/path/secret 미보관.

    ``checked_at`` is a timezone-aware ``datetime`` parsed from the verified ``checked_at``
    string; ``checked_at_iso`` is that exact original canonical string (the one bound into the
    receipt hash). ``receipt_sha256`` is the verifier-confirmed stored hash. ``fingerprints_*``
    are frozen ``ArtifactFingerprint`` tuples (each ``sidecar_suffixes`` a tuple) — no mutable
    list/dict reference is retained. A snapshot is an observation, NOT authenticity, approval,
    freshness, or activation authorization."""

    schema_version: int
    receipt_sha256: str
    checked_at: datetime
    checked_at_iso: str
    market: str
    symbol: str
    enabled: bool
    machine_outcome: str
    inspection_outcome: str
    reasons: tuple[str, ...]
    fingerprints_before: tuple[ArtifactFingerprint, ...]
    fingerprints_after: tuple[ArtifactFingerprint, ...]
    activation_authorized: bool
    runtime_activation_outcome: str
    explicit_operator_approval_required: bool
    writers_stopped_manual_confirmation_required: bool


@dataclass(frozen=True)
class VerifiedPrecheckReceiptResult:
    """Snapshot build verdict — VALID이면 immutable ``receipt``, INVALID이면 ``None``."""

    outcome: VerifiedReceiptSnapshotOutcome
    reasons: tuple[str, ...]
    receipt: VerifiedPrecheckReceipt | None


def verify_and_snapshot_precheck_receipt(payload: object) -> VerifiedPrecheckReceiptResult:
    """Verify an untrusted receipt payload once and freeze it into an immutable snapshot.

    Reuses ``verify_runtime_precheck_receipt_payload``; on INVALID returns the single stable
    ``receipt_snapshot_invalid`` reason. On VALID it copies every retained field into an
    immutable value immediately, so a later mutation of the raw ``payload`` dict cannot change
    the snapshot. Any post-VALID structural surprise (defensive only — unreachable on a truly
    VALID payload) also fails closed to ``receipt_snapshot_invalid``. No raw key/value,
    exception, or path is surfaced."""

    verification = verify_runtime_precheck_receipt_payload(payload)
    if verification.outcome is not ReceiptVerificationOutcome.VALID:
        return _invalid()

    # verifier VALID guarantees a dict with strictly validated fields; copy them defensively.
    assert isinstance(payload, dict)
    assert verification.receipt_sha256 is not None
    assert verification.schema_version is not None
    snapshot = _snapshot_from_verified_payload(
        payload=payload,
        receipt_sha256=verification.receipt_sha256,
        schema_version=verification.schema_version,
    )
    if snapshot is None:
        return _invalid()

    return VerifiedPrecheckReceiptResult(
        outcome=VerifiedReceiptSnapshotOutcome.VALID,
        reasons=(),
        receipt=snapshot,
    )


def _snapshot_from_verified_payload(
    *,
    payload: dict[str, Any],
    receipt_sha256: str,
    schema_version: int,
) -> VerifiedPrecheckReceipt | None:
    checked_at_iso = payload["checked_at"]
    checked_at = _parse_aware(checked_at_iso)
    if checked_at is None:
        return None

    market = payload["market"]
    symbol = payload["symbol"]
    machine_outcome = payload["machine_outcome"]
    inspection_outcome = payload["inspection_outcome"]
    if not (
        type(market) is str
        and type(symbol) is str
        and type(machine_outcome) is str
        and type(inspection_outcome) is str
    ):
        return None

    enabled = strict_bool(payload["enabled"])
    if enabled is None:
        return None

    reasons_raw = payload["reasons"]
    if not isinstance(reasons_raw, list) or any(type(r) is not str for r in reasons_raw):
        return None
    reasons = tuple(reasons_raw)

    before_dicts, before_err = parse_fingerprint_list(payload["fingerprints_before"])
    if before_err is not None:
        return None
    after_dicts, after_err = parse_fingerprint_list(payload["fingerprints_after"])
    if after_err is not None:
        return None

    posture = _verified_activation_posture(payload)
    if posture is None:
        return None
    (
        activation_authorized,
        runtime_activation_outcome,
        approval_required,
        writers_stopped_required,
    ) = posture

    return VerifiedPrecheckReceipt(
        schema_version=schema_version,
        receipt_sha256=receipt_sha256,
        checked_at=checked_at,
        checked_at_iso=checked_at_iso,
        market=market,
        symbol=symbol,
        enabled=enabled,
        machine_outcome=machine_outcome,
        inspection_outcome=inspection_outcome,
        reasons=reasons,
        fingerprints_before=tuple(_fingerprint_from_dict(fp) for fp in before_dicts),
        fingerprints_after=tuple(_fingerprint_from_dict(fp) for fp in after_dicts),
        activation_authorized=activation_authorized,
        runtime_activation_outcome=runtime_activation_outcome,
        explicit_operator_approval_required=approval_required,
        writers_stopped_manual_confirmation_required=writers_stopped_required,
    )


def _verified_activation_posture(
    payload: dict[str, Any],
) -> tuple[bool, str, bool, bool] | None:
    """Snapshot the verified activation posture. ``activation_posture_valid`` already proved
    these equal the canonical NO-GO constants; read them so the snapshot reflects the verified
    payload, failing closed on any surprise."""

    activation_authorized = strict_bool(payload["activation_authorized"])
    approval_required = strict_bool(payload["explicit_operator_approval_required"])
    writers_stopped_required = strict_bool(payload["writers_stopped_manual_confirmation_required"])
    runtime_activation_outcome = payload["runtime_activation_outcome"]
    if (
        activation_authorized is not False
        or approval_required is not True
        or writers_stopped_required is not True
        or runtime_activation_outcome != "no_go"
    ):
        return None
    return (False, "no_go", True, True)


def _fingerprint_from_dict(fp: dict[str, Any]) -> ArtifactFingerprint:
    return ArtifactFingerprint(
        name=fp["name"],
        present=fp["present"],
        is_regular_file=fp["is_regular_file"],
        size=fp["size"],
        sha256=fp["sha256"],
        user_version=fp["user_version"],
        sidecar_suffixes=tuple(fp["sidecar_suffixes"]),
    )


def _parse_aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _invalid() -> VerifiedPrecheckReceiptResult:
    return VerifiedPrecheckReceiptResult(
        outcome=VerifiedReceiptSnapshotOutcome.INVALID,
        reasons=("receipt_snapshot_invalid",),
        receipt=None,
    )
