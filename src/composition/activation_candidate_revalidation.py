"""Approval-time activation candidate state revalidation (RTM-7c.4g).

검증된 PASS receipt와 현재 on-disk artifact 상태를 read-only로 재대조한다.
Operator approval, writer-stop 증명, freshness, receipt authenticity, activation은
범위 밖이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from composition import sqlite_inspector
from composition.paper_fast_loop import PaperFastLoopPaths
from composition.paper_fast_loop_artifacts import PAPER_FAST_LOOP_ARTIFACT_SPECS
from composition.precheck_receipt_schema import (
    PRECHECK_RECEIPT_ARTIFACT_NAMES,
    parse_fingerprint_list,
    strict_bool,
)
from composition.precheck_receipt_verifier import (
    ReceiptVerificationOutcome,
    verify_runtime_precheck_receipt_payload,
)
from config.settings import RuntimePaperFastLoopSettings

__all__ = [
    "ActivationCandidateRevalidationOutcome",
    "ActivationCandidateRevalidationResult",
    "revalidate_activation_candidate",
]

# precheck와 동일한 4-artifact 순서·속성 — 단일 출처(paper_fast_loop_artifacts)에서 파생.
_REVALIDATION_ARTIFACTS: tuple[tuple[str, str, bool], ...] = tuple(
    (spec.name, spec.path_attr, spec.is_sqlite) for spec in PAPER_FAST_LOOP_ARTIFACT_SPECS
)

_ACTIVATION_POSTURE: dict[str, object] = {
    "activation_authorized": False,
    "runtime_activation_outcome": "no_go",
    "explicit_operator_approval_required": True,
    "writers_stopped_manual_confirmation_required": True,
    "freshness_evaluated": False,
}


class ActivationCandidateRevalidationOutcome(StrEnum):
    PASS = "pass"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ActivationCandidateRevalidationResult:
    """Approval 직전 기계적 재검증 verdict — receipt 원문/fingerprint payload 미보관."""

    outcome: ActivationCandidateRevalidationOutcome
    receipt_sha256: str | None
    market: str | None
    symbol: str | None
    reasons: tuple[str, ...]
    current_fingerprints_before: tuple[sqlite_inspector.ArtifactFingerprint, ...]
    current_fingerprints_after: tuple[sqlite_inspector.ArtifactFingerprint, ...]
    activation_authorized: bool
    runtime_activation_outcome: str
    explicit_operator_approval_required: bool
    writers_stopped_manual_confirmation_required: bool
    freshness_evaluated: bool


def revalidate_activation_candidate(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt_payload: object,
    base_dir: str | Path | None = None,
) -> ActivationCandidateRevalidationResult:
    """검증된 PASS receipt와 현재 artifact·config 상태를 read-only로 재대조한다.

    별도 clock read 없음. SQLite connection 없음. activation authorization 없음."""

    resolved_base = Path(".") if base_dir is None else Path(base_dir)
    empty_fps: tuple[sqlite_inspector.ArtifactFingerprint, ...] = ()

    verification = verify_runtime_precheck_receipt_payload(receipt_payload)
    if verification.outcome is not ReceiptVerificationOutcome.VALID:
        return _no_go(
            reason="candidate_receipt_invalid",
            receipt_sha256=verification.receipt_sha256,
            market=None,
            symbol=None,
            current_before=empty_fps,
            current_after=empty_fps,
        )

    assert isinstance(receipt_payload, dict)
    payload = receipt_payload

    if not _is_machine_pass_receipt(payload):
        return _no_go(
            reason="candidate_receipt_not_pass",
            receipt_sha256=verification.receipt_sha256,
            market=_optional_str(payload.get("market")),
            symbol=_optional_str(payload.get("symbol")),
            current_before=empty_fps,
            current_after=empty_fps,
        )

    config_reason = _config_binding_reason(settings=settings, payload=payload)
    if config_reason is not None:
        return _no_go(
            reason=config_reason,
            receipt_sha256=verification.receipt_sha256,
            market=_optional_str(payload.get("market")),
            symbol=_optional_str(payload.get("symbol")),
            current_before=empty_fps,
            current_after=empty_fps,
        )

    receipt_after, err = parse_fingerprint_list(payload["fingerprints_after"])
    if err is not None:
        # verifier VALID 이후이므로 도달 불가 — fail-closed.
        return _no_go(
            reason="candidate_receipt_invalid",
            receipt_sha256=verification.receipt_sha256,
            market=_optional_str(payload.get("market")),
            symbol=_optional_str(payload.get("symbol")),
            current_before=empty_fps,
            current_after=empty_fps,
        )
    receipt_target = tuple(_fingerprint_from_dict(fp) for fp in receipt_after)

    paths = PaperFastLoopPaths.from_settings(settings, base_dir=resolved_base)
    current_before, before_unreadable = _fingerprint_artifacts_fail_closed(paths)
    if before_unreadable:
        return _no_go(
            reasons=before_unreadable,
            receipt_sha256=verification.receipt_sha256,
            market=settings.market,
            symbol=settings.symbol,
            current_before=empty_fps,
            current_after=empty_fps,
        )
    assert current_before is not None
    current_after, after_unreadable = _fingerprint_artifacts_fail_closed(paths)
    if after_unreadable:
        return _no_go(
            reasons=after_unreadable,
            receipt_sha256=verification.receipt_sha256,
            market=settings.market,
            symbol=settings.symbol,
            current_before=current_before,
            current_after=empty_fps,
        )
    assert current_after is not None

    artifact_reasons = _artifact_revalidation_reasons(
        current_before=current_before,
        current_after=current_after,
        receipt_target=receipt_target,
    )
    if artifact_reasons:
        return _no_go(
            reasons=artifact_reasons,
            receipt_sha256=verification.receipt_sha256,
            market=settings.market,
            symbol=settings.symbol,
            current_before=current_before,
            current_after=current_after,
        )

    return ActivationCandidateRevalidationResult(
        outcome=ActivationCandidateRevalidationOutcome.PASS,
        receipt_sha256=verification.receipt_sha256,
        market=settings.market,
        symbol=settings.symbol,
        reasons=(),
        current_fingerprints_before=current_before,
        current_fingerprints_after=current_after,
        **_ACTIVATION_POSTURE,  # type: ignore[arg-type]
    )


def _is_machine_pass_receipt(payload: dict[str, object]) -> bool:
    """structurally VALID receipt가 machine PASS observation인지 확인한다."""
    if payload.get("machine_outcome") != "pass":
        return False
    if payload.get("inspection_outcome") != "ok":
        return False
    reasons = payload.get("reasons")
    return isinstance(reasons, list) and len(reasons) == 0


def _config_binding_reason(
    *, settings: RuntimePaperFastLoopSettings, payload: dict[str, object]
) -> str | None:
    if not settings.enabled:
        return "candidate_config_disabled"
    receipt_market = payload.get("market")
    if receipt_market != settings.market:
        return "candidate_market_mismatch"
    receipt_symbol = payload.get("symbol")
    if receipt_symbol != settings.symbol:
        return "candidate_symbol_mismatch"
    receipt_enabled = strict_bool(payload.get("enabled"))
    if receipt_enabled is not True or receipt_enabled != settings.enabled:
        return "candidate_enabled_mismatch"
    return None


def _fingerprint_artifacts_fail_closed(
    paths: PaperFastLoopPaths,
) -> tuple[tuple[sqlite_inspector.ArtifactFingerprint, ...] | None, tuple[str, ...]]:
    """Fingerprint every artifact in canonical order, converting raw-read ``OSError`` into a
    stable ``candidate_artifact_unreadable:<artifact>`` reason (H1 carry-over).

    ``fingerprint_artifact`` reads the artifact's bytes directly; a missing/permission-denied
    path or a TOCTOU race (file replaced/removed between the ``exists``/``is_file`` probe and
    the ``open``) can raise ``OSError`` (``FileNotFoundError`` / ``PermissionError`` / generic).
    Absent paths are NOT errors — they yield an all-``None`` fingerprint, so only a genuine
    read failure produces a reason. The raw exception type/message/path is never surfaced; one
    reason is emitted per failing artifact in canonical order. When any artifact is unreadable
    the partial fingerprints are discarded (``None`` returned) so no synthetic fingerprint is
    ever treated as a healthy observation."""

    fingerprints: list[sqlite_inspector.ArtifactFingerprint] = []
    reasons: list[str] = []
    for name, attr, is_sqlite in _REVALIDATION_ARTIFACTS:
        try:
            fingerprints.append(
                sqlite_inspector.fingerprint_artifact(
                    getattr(paths, attr), name=name, is_sqlite=is_sqlite
                )
            )
        except OSError:
            reasons.append(f"candidate_artifact_unreadable:{name}")
    if reasons:
        return None, tuple(reasons)
    return tuple(fingerprints), ()


def _fingerprint_from_dict(fp: dict[str, Any]) -> sqlite_inspector.ArtifactFingerprint:
    return sqlite_inspector.ArtifactFingerprint(
        name=fp["name"],
        present=fp["present"],
        is_regular_file=fp["is_regular_file"],
        size=fp["size"],
        sha256=fp["sha256"],
        user_version=fp["user_version"],
        sidecar_suffixes=tuple(fp["sidecar_suffixes"]),
    )


def _artifact_revalidation_reasons(
    *,
    current_before: tuple[sqlite_inspector.ArtifactFingerprint, ...],
    current_after: tuple[sqlite_inspector.ArtifactFingerprint, ...],
    receipt_target: tuple[sqlite_inspector.ArtifactFingerprint, ...],
) -> tuple[str, ...]:
    """revalidation window drift와 receipt after-state mismatch reason을 수집한다.

    drift가 root cause를 소유한다 — 동일 artifact에 mismatch reason을 중복하지 않는다.
    ``receipt_target``은 approval-time 비교 대상인 ``fingerprints_after``여야 한다."""

    reasons: list[str] = []
    for before, after, target in zip(current_before, current_after, receipt_target):
        if before != after:
            reasons.append(f"candidate_current_artifact_drift:{before.name}")
        elif after != target:
            reasons.append(f"candidate_receipt_artifact_mismatch:{after.name}")
    return tuple(reasons)


def _optional_str(value: object) -> str | None:
    return value if type(value) is str else None


def _no_go(
    *,
    reason: str | None = None,
    reasons: tuple[str, ...] | None = None,
    receipt_sha256: str | None,
    market: str | None,
    symbol: str | None,
    current_before: tuple[sqlite_inspector.ArtifactFingerprint, ...],
    current_after: tuple[sqlite_inspector.ArtifactFingerprint, ...],
) -> ActivationCandidateRevalidationResult:
    resolved_reasons = reasons if reasons is not None else ((reason,) if reason else ())
    return ActivationCandidateRevalidationResult(
        outcome=ActivationCandidateRevalidationOutcome.NO_GO,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        reasons=resolved_reasons,
        current_fingerprints_before=current_before,
        current_fingerprints_after=current_after,
        **_ACTIVATION_POSTURE,  # type: ignore[arg-type]
    )
