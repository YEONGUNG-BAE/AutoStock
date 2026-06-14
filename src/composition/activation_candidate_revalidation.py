"""Approval-time activation candidate state revalidation (RTM-7c.4g, +7c.4j snapshot).

검증된 PASS receipt와 현재 on-disk artifact 상태를 read-only로 재대조한다.
Operator approval, writer-stop 증명, freshness, receipt authenticity, activation은
범위 밖이다.

RTM-7c.4j: the public ``revalidate_activation_candidate`` now builds an immutable verified
receipt snapshot **once** and delegates to ``revalidate_verified_activation_candidate``; the
snapshot-based core reads only frozen snapshot fields — it runs no verifier and never touches
the raw payload dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from composition import sqlite_inspector
from composition.paper_fast_loop import PaperFastLoopPaths
from composition.paper_fast_loop_artifacts import PAPER_FAST_LOOP_ARTIFACT_SPECS
from composition.verified_precheck_receipt import (
    VerifiedPrecheckReceipt,
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)
from config.settings import RuntimePaperFastLoopSettings

__all__ = [
    "ActivationCandidateRevalidationOutcome",
    "ActivationCandidateRevalidationResult",
    "revalidate_activation_candidate",
    "revalidate_verified_activation_candidate",
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
    """Raw payload 호환 wrapper — 한 번 verify/snapshot 후 snapshot-based core에 위임한다.

    별도 clock read 없음. SQLite connection 없음. activation authorization 없음."""

    snapshot_result = verify_and_snapshot_precheck_receipt(receipt_payload)
    if snapshot_result.outcome is not VerifiedReceiptSnapshotOutcome.VALID:
        empty_fps: tuple[sqlite_inspector.ArtifactFingerprint, ...] = ()
        return _no_go(
            reason="candidate_receipt_invalid",
            receipt_sha256=None,
            market=None,
            symbol=None,
            current_before=empty_fps,
            current_after=empty_fps,
        )
    assert snapshot_result.receipt is not None
    return revalidate_verified_activation_candidate(
        settings=settings, receipt=snapshot_result.receipt, base_dir=base_dir
    )


def revalidate_verified_activation_candidate(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt: VerifiedPrecheckReceipt,
    base_dir: str | Path | None = None,
) -> ActivationCandidateRevalidationResult:
    """Immutable verified snapshot와 현재 artifact·config 상태를 read-only로 재대조한다.

    verifier를 호출하지 않고 raw payload를 읽지 않는다 — snapshot field만 사용한다.
    별도 clock read 없음. SQLite connection 없음. activation authorization 없음."""

    resolved_base = Path(".") if base_dir is None else Path(base_dir)
    empty_fps: tuple[sqlite_inspector.ArtifactFingerprint, ...] = ()

    if not _is_machine_pass_receipt(receipt):
        return _no_go(
            reason="candidate_receipt_not_pass",
            receipt_sha256=receipt.receipt_sha256,
            market=receipt.market,
            symbol=receipt.symbol,
            current_before=empty_fps,
            current_after=empty_fps,
        )

    config_reason = _config_binding_reason(settings=settings, receipt=receipt)
    if config_reason is not None:
        return _no_go(
            reason=config_reason,
            receipt_sha256=receipt.receipt_sha256,
            market=receipt.market,
            symbol=receipt.symbol,
            current_before=empty_fps,
            current_after=empty_fps,
        )

    # snapshot의 fingerprints_after는 이미 frozen ArtifactFingerprint tuple — re-parse 불필요.
    receipt_target = receipt.fingerprints_after

    paths = PaperFastLoopPaths.from_settings(settings, base_dir=resolved_base)
    current_before, before_unreadable = _fingerprint_artifacts_fail_closed(paths)
    if before_unreadable:
        return _no_go(
            reasons=before_unreadable,
            receipt_sha256=receipt.receipt_sha256,
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
            receipt_sha256=receipt.receipt_sha256,
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
            receipt_sha256=receipt.receipt_sha256,
            market=settings.market,
            symbol=settings.symbol,
            current_before=current_before,
            current_after=current_after,
        )

    return ActivationCandidateRevalidationResult(
        outcome=ActivationCandidateRevalidationOutcome.PASS,
        receipt_sha256=receipt.receipt_sha256,
        market=settings.market,
        symbol=settings.symbol,
        reasons=(),
        current_fingerprints_before=current_before,
        current_fingerprints_after=current_after,
        **_ACTIVATION_POSTURE,  # type: ignore[arg-type]
    )


def _is_machine_pass_receipt(receipt: VerifiedPrecheckReceipt) -> bool:
    """verified snapshot이 machine PASS observation인지 확인한다."""
    return (
        receipt.machine_outcome == "pass"
        and receipt.inspection_outcome == "ok"
        and len(receipt.reasons) == 0
    )


def _config_binding_reason(
    *, settings: RuntimePaperFastLoopSettings, receipt: VerifiedPrecheckReceipt
) -> str | None:
    if not settings.enabled:
        return "candidate_config_disabled"
    if receipt.market != settings.market:
        return "candidate_market_mismatch"
    if receipt.symbol != settings.symbol:
        return "candidate_symbol_mismatch"
    if receipt.enabled is not True or receipt.enabled != settings.enabled:
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
