"""Time-aware final activation candidate preflight (RTM-7c.4h).

Composes the RTM-7c.4g byte-state revalidation with a fresh, caller-supplied-time machine
precheck so that — even when every artifact byte is identical to the candidate receipt's
post-inspection state — an execution-inputs snapshot or active decision whose validity window
has since opened/closed (or any other current-time precheck NO_GO) is caught.

This lane evaluates **current state time-validity** only. It deliberately does NOT evaluate
receipt age / max-age (``receipt_age_evaluated = False``), nor any freshness policy
(``freshness_policy_evaluated = False``). It does not authenticate the receipt, consume an
Operator approval, assert writer-stop, or activate anything — the activation posture is a
constant NO_GO.

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
    revalidate_activation_candidate,
)
from composition.paper_fast_loop import (
    MachineCheckOutcome,
    RuntimePrecheckResult,
    precheck_runtime,
)
from composition.sqlite_inspector import ArtifactFingerprint
from config.settings import RuntimePaperFastLoopSettings

__all__ = [
    "ActivationCandidateFinalPreflightOutcome",
    "ActivationCandidateFinalPreflightResult",
    "final_preflight_activation_candidate",
]

# 고정 posture — 모든 경로에서 동일. activation/approval/receipt-age/freshness 모두 미평가/미승인.
_FINAL_PREFLIGHT_POSTURE: dict[str, object] = {
    "activation_authorized": False,
    "runtime_activation_outcome": "no_go",
    "explicit_operator_approval_required": True,
    "writers_stopped_manual_confirmation_required": True,
    "current_validity_evaluated": True,
    "receipt_age_evaluated": False,
    "freshness_policy_evaluated": False,
}


class ActivationCandidateFinalPreflightOutcome(StrEnum):
    PASS = "pass"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ActivationCandidateFinalPreflightResult:
    """Time-aware final preflight verdict — receipt 원문/fingerprint payload 미보관.

    ``current_validity_evaluated`` / ``receipt_age_evaluated`` / ``freshness_policy_evaluated``
    describe this lane's *evaluation policy* (it checks current-time validity; it never checks
    receipt age or any freshness policy), not per-call completion. A mechanical PASS is NOT an
    activation authorization."""

    outcome: ActivationCandidateFinalPreflightOutcome
    receipt_sha256: str | None
    market: str | None
    symbol: str | None
    reasons: tuple[str, ...]
    revalidation_result: ActivationCandidateRevalidationResult | None
    current_precheck_result: RuntimePrecheckResult | None
    current_validity_evaluated: bool
    receipt_age_evaluated: bool
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

    ``now`` must be timezone-aware; a naive/malformed ``now`` is a fail-closed
    ``candidate_invalid_now`` NO_GO (the underlying ``precheck_runtime`` is never reached).
    No clock read of its own; no operational DB write; no network/credential/broker."""

    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return _no_go(
            reasons=("candidate_invalid_now",),
            receipt_sha256=None,
            market=None,
            symbol=None,
            revalidation=None,
            precheck=None,
        )

    # Step 1 — 4g byte-state revalidation. NO_GO면 즉시 종결, 기존 stable reason 보존.
    revalidation = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt_payload, base_dir=base_dir
    )
    if revalidation.outcome is not ActivationCandidateRevalidationOutcome.PASS:
        return _no_go(
            reasons=revalidation.reasons,
            receipt_sha256=revalidation.receipt_sha256,
            market=revalidation.market,
            symbol=revalidation.symbol,
            revalidation=revalidation,
            precheck=None,
        )

    # Step 2 — 현재 시각 기준 fresh machine precheck (snapshot/active-decision validity 포함).
    resolved_base = Path(".") if base_dir is None else Path(base_dir)
    precheck = precheck_runtime(settings=settings, now=now, base_dir=resolved_base)

    # Step 3 — fresh precheck verdict. NO_GO면 기존 reason을 raw payload 없이 stable prefix로 전달.
    if precheck.machine_outcome is not MachineCheckOutcome.PASS:
        return _no_go(
            reasons=tuple(f"candidate_current_precheck:{reason}" for reason in precheck.reasons),
            receipt_sha256=revalidation.receipt_sha256,
            market=revalidation.market,
            symbol=revalidation.symbol,
            revalidation=revalidation,
            precheck=precheck,
        )

    # Step 4 — revalidation 이후 fresh precheck 사이 state drift. revalidation PASS는
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
        )

    # Step 5 — mechanical PASS. activation은 여전히 false.
    return ActivationCandidateFinalPreflightResult(
        outcome=ActivationCandidateFinalPreflightOutcome.PASS,
        receipt_sha256=revalidation.receipt_sha256,
        market=revalidation.market,
        symbol=revalidation.symbol,
        reasons=(),
        revalidation_result=revalidation,
        current_precheck_result=precheck,
        **_FINAL_PREFLIGHT_POSTURE,  # type: ignore[arg-type]
    )


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
) -> ActivationCandidateFinalPreflightResult:
    return ActivationCandidateFinalPreflightResult(
        outcome=ActivationCandidateFinalPreflightOutcome.NO_GO,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        reasons=reasons,
        revalidation_result=revalidation,
        current_precheck_result=precheck,
        **_FINAL_PREFLIGHT_POSTURE,  # type: ignore[arg-type]
    )
