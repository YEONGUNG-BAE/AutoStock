"""Time-aware final activation candidate preflight (RTM-7c.4h, +7c.4i time, +7c.4j snapshot).

Composes the RTM-7c.4g byte-state revalidation with a fresh, caller-supplied-time machine
precheck so that — even when every artifact byte is identical to the candidate receipt's
post-inspection state — an execution-inputs snapshot or active decision whose validity window
has since opened/closed (or any other current-time precheck NO_GO) is caught.

When it runs the fresh precheck this lane evaluates **current state time-validity** only.
The per-call ``fresh_precheck_executed`` flag records whether that precheck actually ran
(it does not for short-circuit NO_GOs that return first). RTM-7c.4i adds a policy-neutral
receipt time observation: after a 4g PASS it compares the verified receipt ``checked_at``
against the caller ``now``, recording the exact ``receipt_age_microseconds`` and
fail-closing a **future** ``checked_at`` (``candidate_receipt_time_in_future``);
``receipt_age_evaluated`` flips ``True`` once that comparison runs. It still applies NO
max-age / TTL / freshness threshold (``freshness_policy_evaluated = False``), does not
authenticate the receipt, consume an Operator approval, assert writer-stop, or activate
anything — the activation posture is a constant NO_GO.

RTM-7c.4j: the untrusted ``receipt_payload`` is verified and frozen into a single immutable
snapshot **once** (``verify_and_snapshot_precheck_receipt``); the byte-state revalidation and
the receipt time observation then both read that *same* snapshot via their verified-core
entrypoints. The raw receipt verifier is therefore called exactly once per preflight, and a
cross-stage mutation of the raw payload cannot mix observations (hash from one read, age from
another).

``now`` is supplied by the caller and must be timezone-aware; this module reads no clock of
its own. It opens no operational (read-write) SQLite connection, performs no network/credential
access, dispatches no broker order, and creates no runtime artifact. The composed
``precheck_runtime`` does open the configured databases **read-only** for inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from composition.activation_candidate_revalidation import (
    ActivationCandidateRevalidationOutcome,
    ActivationCandidateRevalidationResult,
    revalidate_verified_activation_candidate,
)
from composition.paper_fast_loop import (
    MachineCheckOutcome,
    RuntimePrecheckResult,
    precheck_runtime,
)
from composition.receipt_time_assessment import (
    ReceiptTimeAssessment,
    ReceiptTimeAssessmentOutcome,
    assess_verified_receipt_time,
)
from composition.sqlite_inspector import ArtifactFingerprint
from composition.verified_precheck_receipt import (
    VerifiedPrecheckReceipt,
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)
from config.settings import RuntimePaperFastLoopSettings

__all__ = [
    "ActivationCandidateFinalPreflightOutcome",
    "ActivationCandidateFinalPreflightResult",
    "final_preflight_activation_candidate",
    "final_preflight_verified_activation_candidate",
]

def _posture(*, fresh_precheck_executed: bool, receipt_age_evaluated: bool = False) -> dict[str, object]:
    """Activation posture for one return path.

    activation/approval are never granted and freshness is never evaluated (constant).
    ``fresh_precheck_executed`` is ``True`` only when the composed ``precheck_runtime``
    actually ran (current snapshot/active-decision validity was re-checked at the caller
    ``now``), ``False`` for every short-circuit that returns before it (invalid ``now``, any
    4g revalidation NO_GO). ``receipt_age_evaluated`` is ``True`` once the verified receipt's
    ``checked_at`` was compared against ``now`` (RTM-7c.4i observation: VALID age or
    future-receipt NO_GO), ``False`` before that comparison. ``freshness_policy_evaluated``
    is constant ``False`` — no TTL / max-age / freshness threshold is applied."""

    return {
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
        "fresh_precheck_executed": fresh_precheck_executed,
        "receipt_age_evaluated": receipt_age_evaluated,
        "freshness_policy_evaluated": False,
    }


class ActivationCandidateFinalPreflightOutcome(StrEnum):
    PASS = "pass"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ActivationCandidateFinalPreflightResult:
    """Time-aware final preflight verdict — receipt 원문/fingerprint payload 미보관.

    ``fresh_precheck_executed`` is a *per-call* fact: ``True`` iff the composed
    ``precheck_runtime`` actually ran and re-checked current snapshot/active-decision
    time-validity at the caller ``now``; ``False`` for every short-circuit that returns
    before it (invalid ``now``, any 4g revalidation NO_GO). ``receipt_age_evaluated`` is
    ``True`` once the verified receipt ``checked_at`` was compared against ``now`` (RTM-7c.4i
    policy-neutral observation), with ``receipt_age_microseconds`` the exact ``now - checked_at``
    integer microseconds (``>= 0``) — or ``None`` for a future receipt / pre-comparison
    short-circuit. ``freshness_policy_evaluated`` is constant ``False`` — this lane evaluates
    no TTL / max-age / freshness threshold. A mechanical PASS is NOT an activation
    authorization."""

    outcome: ActivationCandidateFinalPreflightOutcome
    receipt_sha256: str | None
    market: str | None
    symbol: str | None
    reasons: tuple[str, ...]
    revalidation_result: ActivationCandidateRevalidationResult | None
    current_precheck_result: RuntimePrecheckResult | None
    fresh_precheck_executed: bool
    receipt_age_evaluated: bool
    receipt_age_microseconds: int | None
    receipt_time_assessment: ReceiptTimeAssessment | None
    freshness_policy_evaluated: bool
    activation_authorized: bool
    runtime_activation_outcome: str
    explicit_operator_approval_required: bool
    writers_stopped_manual_confirmation_required: bool


def final_preflight_activation_candidate(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt_payload: object,
    now: datetime,
    base_dir: str | Path | None = None,
) -> ActivationCandidateFinalPreflightResult:
    """Run 4g byte-state revalidation then a fresh current-time machine precheck, binding the
    result back to the candidate receipt's post-inspection artifact state.

    ``now`` must be a timezone-aware ``datetime``; any non-``datetime`` (``None``, ``str``,
    ``int``), a naive ``datetime``, a ``None`` UTC offset, or a ``tzinfo`` whose
    ``utcoffset`` raises is a fail-closed ``candidate_invalid_now`` NO_GO (the underlying
    ``precheck_runtime`` is never reached; no raw exception/type/repr escapes).
    No clock read of its own; no operational DB write; no network/credential/broker."""

    if _now_is_invalid(now):
        return _no_go(
            reasons=("candidate_invalid_now",),
            receipt_sha256=None,
            market=None,
            symbol=None,
            revalidation=None,
            precheck=None,
            fresh_precheck_executed=False,
            receipt_time_assessment=None,
        )

    # Step 1 — raw payload을 immutable verified snapshot으로 한 번만 동결 (RTM-7c.4j).
    # 이후 모든 단계(revalidation, receipt time)는 이 snapshot만 사용한다 — verifier는
    # 한 번만 호출되고, raw payload는 다시 접근하지 않는다(cross-stage mutation 차단).
    snapshot_result = verify_and_snapshot_precheck_receipt(receipt_payload)
    if snapshot_result.outcome is not VerifiedReceiptSnapshotOutcome.VALID:
        return _no_go(
            reasons=("candidate_receipt_invalid",),
            receipt_sha256=None,
            market=None,
            symbol=None,
            revalidation=None,
            precheck=None,
            fresh_precheck_executed=False,
            receipt_time_assessment=None,
        )
    assert snapshot_result.receipt is not None

    return final_preflight_verified_activation_candidate(
        settings=settings,
        receipt=snapshot_result.receipt,
        now=now,
        base_dir=base_dir,
    )


def final_preflight_verified_activation_candidate(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt: VerifiedPrecheckReceipt,
    now: datetime,
    base_dir: str | Path | None = None,
) -> ActivationCandidateFinalPreflightResult:
    """Verified snapshot 기반 final preflight core — verifier/raw payload 접근 0.

    RTM-7c.4g revalidation, receipt-time observation, fresh precheck, post-revalidation
    drift를 동일 immutable snapshot 위에서 실행한다. freshness policy는 평가하지 않는다."""

    # Step 2 — snapshot-based 4g byte-state revalidation. NO_GO면 즉시 종결, 기존 stable reason 보존.
    revalidation = revalidate_verified_activation_candidate(
        settings=settings, receipt=receipt, base_dir=base_dir
    )
    if revalidation.outcome is not ActivationCandidateRevalidationOutcome.PASS:
        return _no_go(
            reasons=revalidation.reasons,
            receipt_sha256=revalidation.receipt_sha256,
            market=revalidation.market,
            symbol=revalidation.symbol,
            revalidation=revalidation,
            precheck=None,
            fresh_precheck_executed=False,
            receipt_time_assessment=None,
        )

    # Step 3 — snapshot-based policy-neutral receipt time observation (RTM-7c.4i). 4g PASS는
    # aware checked_at을 가진 VALID snapshot을 보장하므로, 여기서 도달 가능한 NO_GO는
    # future checked_at 뿐이다. TTL/max-age/freshness threshold는 적용하지 않는다.
    time_assessment = assess_verified_receipt_time(receipt=receipt, now=now)
    if time_assessment.outcome is not ReceiptTimeAssessmentOutcome.VALID:
        reason = (
            "candidate_receipt_time_in_future"
            if "receipt_time_in_future" in time_assessment.reasons
            else "candidate_receipt_time_invalid"
        )
        return _no_go(
            reasons=(reason,),
            receipt_sha256=revalidation.receipt_sha256,
            market=revalidation.market,
            symbol=revalidation.symbol,
            revalidation=revalidation,
            precheck=None,
            fresh_precheck_executed=False,
            receipt_age_evaluated=time_assessment.receipt_age_evaluated,
            receipt_age_microseconds=time_assessment.receipt_age_microseconds,
            receipt_time_assessment=time_assessment,
        )
    receipt_age_microseconds = time_assessment.receipt_age_microseconds

    # Step 4 — 현재 시각 기준 fresh machine precheck (snapshot/active-decision validity 포함).
    resolved_base = Path(".") if base_dir is None else Path(base_dir)
    precheck = precheck_runtime(settings=settings, now=now, base_dir=resolved_base)

    # Step 5 — fresh precheck verdict. NO_GO면 기존 reason을 raw payload 없이 stable prefix로 전달.
    if precheck.machine_outcome is not MachineCheckOutcome.PASS:
        return _no_go(
            reasons=tuple(f"candidate_current_precheck:{reason}" for reason in precheck.reasons),
            receipt_sha256=revalidation.receipt_sha256,
            market=revalidation.market,
            symbol=revalidation.symbol,
            revalidation=revalidation,
            precheck=precheck,
            fresh_precheck_executed=True,
            receipt_age_evaluated=True,
            receipt_age_microseconds=receipt_age_microseconds,
            receipt_time_assessment=time_assessment,
        )

    # Step 6 — revalidation 이후 fresh precheck 사이 state drift. revalidation PASS는
    # current-after == receipt fingerprints_after를 보장하므로 그 값을 비교 기준으로 쓴다.
    drift_reasons = _post_revalidation_drift_reasons(
        revalidation_after=revalidation.current_fingerprints_after,
        fresh_after=precheck.receipt.fingerprints_after,
    )
    if drift_reasons:
        return _no_go(
            reasons=drift_reasons,
            receipt_sha256=revalidation.receipt_sha256,
            market=revalidation.market,
            symbol=revalidation.symbol,
            revalidation=revalidation,
            precheck=precheck,
            fresh_precheck_executed=True,
            receipt_age_evaluated=True,
            receipt_age_microseconds=receipt_age_microseconds,
            receipt_time_assessment=time_assessment,
        )

    # Step 7 — mechanical PASS. activation은 여전히 false.
    return ActivationCandidateFinalPreflightResult(
        outcome=ActivationCandidateFinalPreflightOutcome.PASS,
        receipt_sha256=revalidation.receipt_sha256,
        market=revalidation.market,
        symbol=revalidation.symbol,
        reasons=(),
        revalidation_result=revalidation,
        current_precheck_result=precheck,
        receipt_age_microseconds=receipt_age_microseconds,
        receipt_time_assessment=time_assessment,
        **_posture(fresh_precheck_executed=True, receipt_age_evaluated=True),  # type: ignore[arg-type]
    )


def _now_is_invalid(now: object) -> bool:
    """Strict fail-closed ``now`` guard.

    Returns ``True`` (invalid) for any non-``datetime`` (``None``/``str``/``int``), a naive
    ``datetime``, a ``None`` UTC offset, or a ``tzinfo`` whose ``utcoffset`` raises. The
    raising case is swallowed so no exception/type/repr escapes the API contract."""

    if not isinstance(now, datetime):
        return True
    try:
        offset = now.utcoffset()
    except Exception:
        return True
    return offset is None


def _post_revalidation_drift_reasons(
    *,
    revalidation_after: tuple[ArtifactFingerprint, ...],
    fresh_after: tuple[ArtifactFingerprint, ...],
) -> tuple[str, ...]:
    """Per-artifact drift between the revalidation-time state and the fresh-precheck state.

    ``revalidation_after`` is the candidate receipt's post-inspection fingerprint set (4g PASS
    guarantees current-after equals receipt ``fingerprints_after``). Any artifact that differs
    from the fresh precheck's post-inspection fingerprint changed between the two reads. One
    reason per artifact in canonical order; the fresh precheck's own within-window drift is
    owned by ``precheck_artifact_changed:<artifact>`` (Step 3), not duplicated here."""

    reasons: list[str] = []
    for fresh, original in zip(fresh_after, revalidation_after):
        if fresh != original:
            reasons.append(f"candidate_post_revalidation_artifact_drift:{fresh.name}")
    return tuple(reasons)


def _no_go(
    *,
    reasons: tuple[str, ...],
    receipt_sha256: str | None,
    market: str | None,
    symbol: str | None,
    revalidation: ActivationCandidateRevalidationResult | None,
    precheck: RuntimePrecheckResult | None,
    fresh_precheck_executed: bool,
    receipt_age_evaluated: bool = False,
    receipt_age_microseconds: int | None = None,
    receipt_time_assessment: ReceiptTimeAssessment | None = None,
) -> ActivationCandidateFinalPreflightResult:
    return ActivationCandidateFinalPreflightResult(
        outcome=ActivationCandidateFinalPreflightOutcome.NO_GO,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        reasons=reasons,
        revalidation_result=revalidation,
        current_precheck_result=precheck,
        receipt_age_microseconds=receipt_age_microseconds,
        receipt_time_assessment=receipt_time_assessment,
        **_posture(  # type: ignore[arg-type]
            fresh_precheck_executed=fresh_precheck_executed,
            receipt_age_evaluated=receipt_age_evaluated,
        ),
    )
