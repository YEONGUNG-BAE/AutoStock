"""Offline two-stage forward monthly observation harness.

PREPARE freezes a sanitized target-weight decision before the observation
month is present in the sibling dataset.  FINALIZE verifies that immutable
decision and evaluates exactly the cutoff-to-observation-month interval.

This module is deliberately outside runtime composition.  It neither imports
runtime configuration nor starts paper/live processes, and every artifact is
required to resolve outside the repository.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_engine.local_dataset import (
    LocalMonthlyDatasetAssemblyResult,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_evaluation import (
    LOCAL_PRODUCT_RELATIVE_V1_NEUTRAL_BASELINE_POLICY_V1,
    LOCAL_PRODUCT_RELATIVE_V1_NEUTRAL_BASELINE_WEIGHTS_V1,
    LOCAL_RULES_ALLOCATOR_V2_STATIC_NORMAL_STATE_POLICY,
    LOCAL_STATIC_NEUTRAL_BASELINE_POLICY_V1,
    LOCAL_STATIC_NEUTRAL_BASELINE_WEIGHTS_V1,
)
from backtest_engine.rebalance import COST_MODEL_V1
from backtest_engine.rules_allocator import (
    RULES_ALLOCATOR_V2_POLICY,
    allocate_rules_v2_target_weights,
)

FORWARD_MONTHLY_OBSERVATION_POLICY_V1 = "offline_forward_monthly_observation.v1"
FORWARD_SNAPSHOT_INTEGRITY_POLICY_V1 = "canonical_json_sha256.v1"

FORWARD_OBSERVATION_START = "2026-08"
FORWARD_OBSERVATION_END = "2027-07"
FORWARD_OBSERVATION_WINDOW = "2026-08 through 2027-07 inclusive"
FORWARD_MINIMUM_OBSERVATIONS = 12
FORWARD_NORMALIZED_INITIAL_PORTFOLIO_VALUE_KRW = Decimal("100000000")

FORWARD_CANDIDATE_ALLOCATOR_VERSION = RULES_ALLOCATOR_V2_POLICY
FORWARD_CANDIDATE_STATE_POLICY = (
    LOCAL_RULES_ALLOCATOR_V2_STATIC_NORMAL_STATE_POLICY
)
FORWARD_PRIMARY_BENCHMARK = "S&P 500 TR KRW-unhedged"
FORWARD_IMPLEMENTED_US60_POLICY = LOCAL_STATIC_NEUTRAL_BASELINE_POLICY_V1
FORWARD_PRODUCT_RELATIVE_V1_POLICY = (
    LOCAL_PRODUCT_RELATIVE_V1_NEUTRAL_BASELINE_POLICY_V1
)
FORWARD_COST_MODEL_VERSION = COST_MODEL_V1

FORWARD_FEE_BPS = Decimal("10")
FORWARD_KR_SELL_TAX_BPS = Decimal("23")
FORWARD_FX_SPREAD_BPS = Decimal("15")
_BPS = Decimal("10000")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_FX_ASSET_IDS = frozenset({"asset_us", "asset_gold"})
_REQUIRED_ASSET_IDS = ("asset_us", "asset_kr", "asset_gold")

EvidenceStatus = Literal[
    "PENDING_MONTHLY_OBSERVATION",
    "PENDING_FULL_WINDOW",
    "BLOCKED_DATA_QUALITY",
    "BLOCKED_NAV_SANITY",
    "BLOCKED_FREQUENCY_ALIGNMENT",
    "COMPLETE_FULL_WINDOW_READY_FOR_GATE_REVIEW",
]


class ForwardObservationError(ValueError):
    """Base class for sanitized forward-observation contract failures."""


class ForwardSnapshotIntegrityError(ForwardObservationError):
    """Raised when a frozen decision snapshot is missing or modified."""


class ForwardDataQualityError(ForwardObservationError):
    """Raised when completed observation data fails deterministic checks."""


class ForwardFrequencyAlignmentError(ForwardObservationError):
    """Raised when the requested monthly interval is not exactly one month."""


class ForwardNavSanityError(ForwardObservationError):
    """Raised when a computed normalized portfolio value is not sane."""


class ForwardStaticComparatorSeparationError(ForwardDataQualityError):
    """Raised when frozen comparator identities or weights collapse together."""


class SanitizedTargetWeight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    weight: Decimal

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("asset_id must not be blank.")
        return normalized

    @field_validator("weight", mode="before")
    @classmethod
    def validate_weight(cls, value: Any) -> Decimal:
        parsed = _decimal(value, field_name="weight")
        if parsed < _ZERO or parsed > _ONE:
            raise ValueError("weight must be between 0 and 1.")
        return parsed


class FrozenForwardDecisionSnapshot(BaseModel):
    """Sanitized immutable PREPARE payload, excluding its envelope digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_policy: Literal["offline_forward_monthly_observation.v1"]
    report_month: str
    decision_cutoff_period: str
    observation_index: str
    observation_window: Literal["2026-08 through 2027-07 inclusive"]
    minimum_observations: Literal[12]
    expected_git_main: str
    candidate_allocator_version: Literal[
        "local_monthly_rules_allocator_v2_contract.sp_core_relative_recovery.v1"
    ]
    candidate_state_policy: Literal[
        "local_monthly_rules_allocator_v2_static_normal_state.v1"
    ]
    primary_benchmark: Literal["S&P 500 TR KRW-unhedged"]
    implemented_us60_static_policy: Literal[
        "local_monthly_static_neutral_baseline_us60_kr20_gold15_cash5.v1"
    ]
    product_relative_v1_neutral_policy: Literal[
        "static_v1_neutral_baseline_cash20_kr40_us24_gold16.v1"
    ]
    candidate_target_weights: tuple[SanitizedTargetWeight, ...]
    implemented_us60_static_target_weights: tuple[SanitizedTargetWeight, ...]
    product_relative_v1_neutral_target_weights: tuple[SanitizedTargetWeight, ...]
    normalized_initial_portfolio_value_krw: Decimal
    normalized_initial_portfolio_state: Literal["all_cash_before_frozen_decision"]
    cost_model_version: Literal["simple_proportional_fee_sell_tax_fx_spread.v1"]
    decision_input_digest: str

    @field_validator("report_month", "decision_cutoff_period")
    @classmethod
    def validate_period(cls, value: str) -> str:
        _parse_period(value)
        return value

    @field_validator("expected_git_main", "decision_input_digest")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 40 and len(normalized) != 64:
            raise ValueError("digest must be a 40- or 64-character hexadecimal value.")
        try:
            int(normalized, 16)
        except ValueError as exc:
            raise ValueError("digest must be hexadecimal.") from exc
        return normalized

    @field_validator("normalized_initial_portfolio_value_krw", mode="before")
    @classmethod
    def validate_initial_value(cls, value: Any) -> Decimal:
        parsed = _decimal(value, field_name="normalized_initial_portfolio_value_krw")
        if parsed != FORWARD_NORMALIZED_INITIAL_PORTFOLIO_VALUE_KRW:
            raise ValueError("normalized initial portfolio value is frozen.")
        return parsed

    @model_validator(mode="after")
    def validate_contract(self) -> "FrozenForwardDecisionSnapshot":
        expected_index = _observation_index(self.report_month)
        if self.observation_index != f"{expected_index} of 12":
            raise ValueError("observation_index does not match report_month.")
        if self.decision_cutoff_period != _previous_period(self.report_month):
            raise ValueError("decision_cutoff_period must precede report_month by one month.")
        for label, weights in (
            ("candidate", self.candidate_target_weights),
            ("implemented US60", self.implemented_us60_static_target_weights),
            ("product-relative V1", self.product_relative_v1_neutral_target_weights),
        ):
            _validate_weights(weights, label=label)
        return self


class ForwardPrepareResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_path: str
    snapshot_sha256: str
    snapshot: FrozenForwardDecisionSnapshot


class ForwardFinalizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metrics_path: str
    manifest_path: str
    evidence_status: EvidenceStatus
    metrics: dict[str, Any]


def prepare_forward_monthly_observation(
    *,
    repo_root: Path,
    data_root: Path,
    output_root: Path,
    report_month: str,
    expected_git_main: str,
    candidate_allocator_version: str,
    safe_overwrite: bool = False,
    today: date | None = None,
    observed_git_head: str | None = None,
) -> ForwardPrepareResult:
    """Freeze one sanitized next-month decision outside the repository."""

    resolved_repo, resolved_output = _validated_external_roots(
        repo_root=repo_root,
        output_root=output_root,
    )
    expected_sha = _validate_expected_identity(
        repo_root=resolved_repo,
        expected_git_main=expected_git_main,
        candidate_allocator_version=candidate_allocator_version,
        observed_git_head=observed_git_head,
    )
    cutoff = _previous_period(report_month)
    index = _observation_index(report_month)
    dataset = _assemble_dataset(repo_root=resolved_repo, data_root=data_root)
    _validate_prepare_dataset(dataset, cutoff=cutoff, report_month=report_month)

    candidate = allocate_rules_v2_target_weights()
    snapshot = FrozenForwardDecisionSnapshot(
        observation_policy=FORWARD_MONTHLY_OBSERVATION_POLICY_V1,
        report_month=report_month,
        decision_cutoff_period=cutoff,
        observation_index=f"{index} of 12",
        observation_window=FORWARD_OBSERVATION_WINDOW,
        minimum_observations=FORWARD_MINIMUM_OBSERVATIONS,
        expected_git_main=expected_sha,
        candidate_allocator_version=candidate.allocator_version,
        candidate_state_policy=FORWARD_CANDIDATE_STATE_POLICY,
        primary_benchmark=FORWARD_PRIMARY_BENCHMARK,
        implemented_us60_static_policy=FORWARD_IMPLEMENTED_US60_POLICY,
        product_relative_v1_neutral_policy=FORWARD_PRODUCT_RELATIVE_V1_POLICY,
        candidate_target_weights=tuple(
            SanitizedTargetWeight(asset_id=item.asset_id, weight=item.weight)
            for item in candidate.weights
        ),
        implemented_us60_static_target_weights=_sanitized_weights(
            LOCAL_STATIC_NEUTRAL_BASELINE_WEIGHTS_V1
        ),
        product_relative_v1_neutral_target_weights=_sanitized_weights(
            LOCAL_PRODUCT_RELATIVE_V1_NEUTRAL_BASELINE_WEIGHTS_V1
        ),
        normalized_initial_portfolio_value_krw=(
            FORWARD_NORMALIZED_INITIAL_PORTFOLIO_VALUE_KRW
        ),
        normalized_initial_portfolio_state="all_cash_before_frozen_decision",
        cost_model_version=FORWARD_COST_MODEL_VERSION,
        decision_input_digest=_decision_input_digest(dataset, cutoff=cutoff),
    )
    payload = _snapshot_payload(snapshot)
    digest = _payload_sha256(payload)
    document = {
        **payload,
        "integrity": {
            "policy": FORWARD_SNAPSHOT_INTEGRITY_POLICY_V1,
            "sha256": digest,
        },
    }
    snapshot_path = resolved_output / f"forward_{report_month}_decision.backtest.json"
    _write_external_json(
        path=snapshot_path,
        payload=document,
        repo_root=resolved_repo,
        overwrite=safe_overwrite,
        overwrite_deadline=_period_start(report_month),
        today=today or date.today(),
    )
    return ForwardPrepareResult(
        snapshot_path=str(snapshot_path),
        snapshot_sha256=digest,
        snapshot=snapshot,
    )


def finalize_forward_monthly_observation(
    *,
    repo_root: Path,
    data_root: Path,
    output_root: Path,
    decision_snapshot_path: Path,
    expected_git_main: str,
    candidate_allocator_version: str,
    safe_overwrite: bool = False,
    today: date | None = None,
    observed_git_head: str | None = None,
) -> ForwardFinalizeResult:
    """Verify a frozen decision and emit one sanitized monthly observation."""

    resolved_repo, resolved_output = _validated_external_roots(
        repo_root=repo_root,
        output_root=output_root,
    )
    expected_sha = _validate_expected_identity(
        repo_root=resolved_repo,
        expected_git_main=expected_git_main,
        candidate_allocator_version=candidate_allocator_version,
        observed_git_head=observed_git_head,
    )
    snapshot, snapshot_sha = load_and_verify_forward_decision_snapshot(
        decision_snapshot_path,
        repo_root=resolved_repo,
        output_root=resolved_output,
    )
    _verify_frozen_identity(
        snapshot,
        expected_git_main=expected_sha,
        candidate_allocator_version=candidate_allocator_version,
    )

    statuses = {
        "nav_sanity_status": "PENDING",
        "dataset_sanity_status": "PENDING",
        "frequency_alignment_status": "PENDING",
        "static_comparator_separation_status": "PENDING",
    }
    monthly: dict[str, Decimal | None] = {
        "candidate": None,
        "primary": None,
        "us60": None,
        "product_v1": None,
    }
    evidence_status: EvidenceStatus = "PENDING_MONTHLY_OBSERVATION"
    dataset: LocalMonthlyDatasetAssemblyResult | None = None
    current_day = today or date.today()

    try:
        if current_day < _next_period_start(snapshot.report_month):
            raise _IncompleteObservation("observation month is not complete.")
        dataset = _assemble_dataset(repo_root=resolved_repo, data_root=data_root)
        if snapshot.report_month not in dataset.common_periods:
            raise _IncompleteObservation("observation month is not complete.")
        if snapshot.decision_cutoff_period not in dataset.common_periods:
            raise _IncompleteObservation("decision cutoff period is unavailable.")
        _validate_frequency(snapshot)
        statuses["frequency_alignment_status"] = "PASS"
        _validate_finalize_dataset(dataset, snapshot=snapshot)
        statuses["dataset_sanity_status"] = "PASS"
        _validate_static_comparator_separation(snapshot)
        statuses["static_comparator_separation_status"] = "PASS"
        monthly = _calculate_monthly_returns(dataset, snapshot=snapshot)
        _validate_nav_sanity(
            monthly,
            initial_value=snapshot.normalized_initial_portfolio_value_krw,
        )
        statuses["nav_sanity_status"] = "PASS"
        evidence_status = (
            "COMPLETE_FULL_WINDOW_READY_FOR_GATE_REVIEW"
            if _observation_index(snapshot.report_month) == FORWARD_MINIMUM_OBSERVATIONS
            else "PENDING_FULL_WINDOW"
        )
    except _IncompleteObservation:
        evidence_status = "PENDING_MONTHLY_OBSERVATION"
    except ForwardFrequencyAlignmentError:
        statuses["frequency_alignment_status"] = "BLOCKED"
        evidence_status = "BLOCKED_FREQUENCY_ALIGNMENT"
    except ForwardNavSanityError:
        statuses["nav_sanity_status"] = "BLOCKED"
        evidence_status = "BLOCKED_NAV_SANITY"
    except ForwardStaticComparatorSeparationError:
        statuses["static_comparator_separation_status"] = "BLOCKED"
        evidence_status = "BLOCKED_DATA_QUALITY"
    except ForwardDataQualityError:
        statuses["dataset_sanity_status"] = "BLOCKED"
        evidence_status = "BLOCKED_DATA_QUALITY"
    except ValueError as exc:
        if "CSV file not found" in str(exc):
            evidence_status = "PENDING_MONTHLY_OBSERVATION"
        else:
            statuses["dataset_sanity_status"] = "BLOCKED"
            evidence_status = "BLOCKED_DATA_QUALITY"

    try:
        cumulative = _cumulative_metrics(
            output_root=resolved_output,
            snapshot=snapshot,
            monthly=monthly,
            evidence_status=evidence_status,
        )
    except ForwardDataQualityError:
        statuses["dataset_sanity_status"] = "BLOCKED"
        evidence_status = "BLOCKED_DATA_QUALITY"
        cumulative = {
            "candidate": None,
            "primary": None,
            "us60": None,
            "product_v1": None,
        }
    if evidence_status == "PENDING_MONTHLY_OBSERVATION":
        cumulative = {key: None for key in cumulative}
    elif (
        _observation_index(snapshot.report_month) > 1
        and any(value is None for value in cumulative.values())
    ):
        evidence_status = "PENDING_MONTHLY_OBSERVATION"
        cumulative = {key: None for key in cumulative}

    metrics = _metrics_payload(
        snapshot=snapshot,
        evidence_status=evidence_status,
        monthly=monthly,
        cumulative=cumulative,
        statuses=statuses,
    )
    metrics_digest = _payload_sha256(metrics)
    metrics_document = {
        **metrics,
        "integrity": {
            "policy": FORWARD_SNAPSHOT_INTEGRITY_POLICY_V1,
            "sha256": metrics_digest,
        },
    }
    metrics_path = resolved_output / (
        f"forward_{snapshot.report_month}_monthly_metrics.backtest.json"
    )
    manifest = {
        "observation_policy": FORWARD_MONTHLY_OBSERVATION_POLICY_V1,
        "report_month": snapshot.report_month,
        "observation_index": snapshot.observation_index,
        "evidence_status": evidence_status,
        "decision_snapshot_sha256": snapshot_sha,
        "monthly_metrics_sha256": metrics_digest,
        "evaluated_observation_count": 1 if monthly["candidate"] is not None else 0,
        "sanitized_output": True,
    }
    manifest_path = resolved_output / (
        f"forward_{snapshot.report_month}_monthly_manifest.backtest.json"
    )
    _write_external_json(
        path=metrics_path,
        payload=metrics_document,
        repo_root=resolved_repo,
        overwrite=safe_overwrite,
        overwrite_deadline=_next_period_start(snapshot.report_month),
        today=current_day,
    )
    _write_external_json(
        path=manifest_path,
        payload=manifest,
        repo_root=resolved_repo,
        overwrite=safe_overwrite,
        overwrite_deadline=_next_period_start(snapshot.report_month),
        today=current_day,
    )
    return ForwardFinalizeResult(
        metrics_path=str(metrics_path),
        manifest_path=str(manifest_path),
        evidence_status=evidence_status,
        metrics=metrics,
    )


def load_and_verify_forward_decision_snapshot(
    path: Path,
    *,
    repo_root: Path,
    output_root: Path,
) -> tuple[FrozenForwardDecisionSnapshot, str]:
    """Load one external snapshot and reject any content modification."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise ForwardSnapshotIntegrityError("original decision snapshot is required.")
    if _is_relative_to(resolved, repo_root.resolve()):
        raise ForwardSnapshotIntegrityError("decision snapshot must be outside repo_root.")
    if not _is_relative_to(resolved, output_root.resolve()):
        raise ForwardSnapshotIntegrityError("decision snapshot must be under output_root.")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardSnapshotIntegrityError("decision snapshot is not valid JSON.") from exc
    if not isinstance(document, dict):
        raise ForwardSnapshotIntegrityError("decision snapshot must be a JSON object.")
    integrity = document.pop("integrity", None)
    if not isinstance(integrity, dict):
        raise ForwardSnapshotIntegrityError("decision snapshot integrity metadata is missing.")
    if integrity.get("policy") != FORWARD_SNAPSHOT_INTEGRITY_POLICY_V1:
        raise ForwardSnapshotIntegrityError("decision snapshot integrity policy changed.")
    expected = integrity.get("sha256")
    actual = _payload_sha256(document)
    if not isinstance(expected, str) or expected != actual:
        raise ForwardSnapshotIntegrityError("decision snapshot digest mismatch.")
    try:
        snapshot = FrozenForwardDecisionSnapshot.model_validate(document)
    except ValueError as exc:
        raise ForwardSnapshotIntegrityError("decision snapshot contract is inconsistent.") from exc
    return snapshot, actual


class _IncompleteObservation(ForwardObservationError):
    pass


def _assemble_dataset(*, repo_root: Path, data_root: Path) -> LocalMonthlyDatasetAssemblyResult:
    return assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=default_local_monthly_instrument_specs_for_kospi_primary(),
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )


def _validate_prepare_dataset(
    dataset: LocalMonthlyDatasetAssemblyResult,
    *,
    cutoff: str,
    report_month: str,
) -> None:
    observed_periods = {_record_period(record.payload) for record in dataset.source_records}
    observed_periods.update(point.period_key for point in dataset.fx_points)
    if any(period >= report_month for period in observed_periods):
        raise ForwardObservationError(
            "PREPARE input must not contain the observation-month outcome."
        )
    if not dataset.common_periods or dataset.common_periods[-1] != cutoff:
        raise ForwardObservationError(
            "PREPARE requires decision_cutoff_period as the latest complete common period."
        )


def _validate_finalize_dataset(
    dataset: LocalMonthlyDatasetAssemblyResult,
    *,
    snapshot: FrozenForwardDecisionSnapshot,
) -> None:
    if _decision_input_digest(dataset, cutoff=snapshot.decision_cutoff_period) != (
        snapshot.decision_input_digest
    ):
        raise ForwardDataQualityError("decision-period input changed after PREPARE.")
    values = _asset_period_values(dataset)
    for asset_id in _REQUIRED_ASSET_IDS:
        for period in (snapshot.decision_cutoff_period, snapshot.report_month):
            if (asset_id, period) not in values:
                raise _IncompleteObservation("required monthly asset observation is missing.")
    fx = {point.period_key: point.usdkrw_rate for point in dataset.fx_points}
    for period in (snapshot.decision_cutoff_period, snapshot.report_month):
        if period not in fx:
            raise _IncompleteObservation("required monthly FX observation is missing.")


def _validate_frequency(snapshot: FrozenForwardDecisionSnapshot) -> None:
    if snapshot.decision_cutoff_period != _previous_period(snapshot.report_month):
        raise ForwardFrequencyAlignmentError(
            "decision cutoff and report month must be consecutive monthly periods."
        )


def _validate_static_comparator_separation(
    snapshot: FrozenForwardDecisionSnapshot,
) -> None:
    policies = {
        snapshot.candidate_allocator_version,
        snapshot.implemented_us60_static_policy,
        snapshot.product_relative_v1_neutral_policy,
    }
    weight_sets = {
        _weight_signature(snapshot.candidate_target_weights),
        _weight_signature(snapshot.implemented_us60_static_target_weights),
        _weight_signature(snapshot.product_relative_v1_neutral_target_weights),
    }
    if len(policies) != 3 or len(weight_sets) != 3:
        raise ForwardStaticComparatorSeparationError(
            "static comparator separation failed."
        )


def _calculate_monthly_returns(
    dataset: LocalMonthlyDatasetAssemblyResult,
    *,
    snapshot: FrozenForwardDecisionSnapshot,
) -> dict[str, Decimal | None]:
    values = _asset_period_values(dataset)
    fx = {point.period_key: point.usdkrw_rate for point in dataset.fx_points}
    cutoff = snapshot.decision_cutoff_period
    report = snapshot.report_month
    asset_returns: dict[str, Decimal] = {}
    for asset_id in _REQUIRED_ASSET_IDS:
        start = values[(asset_id, cutoff)]
        end = values[(asset_id, report)]
        ratio = end / start
        if asset_id in _FX_ASSET_IDS:
            ratio *= fx[report] / fx[cutoff]
        asset_returns[asset_id] = ratio - _ONE

    candidate = _portfolio_monthly_return(
        snapshot.candidate_target_weights,
        asset_returns=asset_returns,
    )
    us60 = _portfolio_monthly_return(
        snapshot.implemented_us60_static_target_weights,
        asset_returns=asset_returns,
    )
    product_v1 = _portfolio_monthly_return(
        snapshot.product_relative_v1_neutral_target_weights,
        asset_returns=asset_returns,
    )
    return {
        "candidate": candidate,
        "primary": asset_returns["asset_us"],
        "us60": us60,
        "product_v1": product_v1,
    }


def _portfolio_monthly_return(
    weights: tuple[SanitizedTargetWeight, ...],
    *,
    asset_returns: dict[str, Decimal],
) -> Decimal:
    weighted_return = sum(
        (
            item.weight * asset_returns[item.asset_id]
            for item in weights
            if item.asset_id != "cash"
        ),
        _ZERO,
    )
    non_cash_weight = sum(
        (item.weight for item in weights if item.asset_id != "cash"), _ZERO
    )
    fx_weight = sum(
        (item.weight for item in weights if item.asset_id in _FX_ASSET_IDS), _ZERO
    )
    initial_cost_drag = (
        non_cash_weight * FORWARD_FEE_BPS + fx_weight * FORWARD_FX_SPREAD_BPS
    ) / _BPS
    return weighted_return - initial_cost_drag


def _validate_nav_sanity(
    monthly: dict[str, Decimal | None],
    *,
    initial_value: Decimal,
) -> None:
    for label, value in monthly.items():
        if value is None or not value.is_finite():
            raise ForwardNavSanityError(f"{label} monthly return must be finite.")
        ending_value = initial_value * (_ONE + value)
        if value <= -_ONE or not ending_value.is_finite() or ending_value <= _ZERO:
            raise ForwardNavSanityError(f"{label} normalized NAV failed sanity.")


def _cumulative_metrics(
    *,
    output_root: Path,
    snapshot: FrozenForwardDecisionSnapshot,
    monthly: dict[str, Decimal | None],
    evidence_status: EvidenceStatus,
) -> dict[str, Decimal | None]:
    names = ("candidate", "primary", "us60", "product_v1")
    if any(monthly[name] is None for name in names):
        return {name: None for name in names}
    index = _observation_index(snapshot.report_month)
    previous = {name: _ZERO for name in names}
    if index > 1:
        previous_month = _previous_period(snapshot.report_month)
        previous_path = output_root / (
            f"forward_{previous_month}_monthly_metrics.backtest.json"
        )
        if not previous_path.is_file():
            return {name: None for name in names}
        previous_doc = _load_verified_json_document(previous_path)
        if previous_doc.get("report_month") != previous_month:
            raise ForwardDataQualityError("previous cumulative ledger month is invalid.")
        if previous_doc.get("evidence_status") not in {
            "PENDING_FULL_WINDOW",
            "COMPLETE_FULL_WINDOW_READY_FOR_GATE_REVIEW",
        }:
            return {name: None for name in names}
        mapping = {
            "candidate": "candidate_cumulative_return_to_date",
            "primary": "primary_benchmark_cumulative_return_to_date",
            "us60": "implemented_us60_static_cumulative_return_to_date",
            "product_v1": "product_relative_v1_neutral_cumulative_return_to_date",
        }
        try:
            previous = {
                name: _decimal(previous_doc[field], field_name=field)
                for name, field in mapping.items()
            }
        except (KeyError, ValueError) as exc:
            raise ForwardDataQualityError("previous cumulative ledger is invalid.") from exc
    if evidence_status.startswith("BLOCKED"):
        return {name: None for name in names}
    return {
        name: (_ONE + previous[name]) * (_ONE + monthly[name]) - _ONE  # type: ignore[operator]
        for name in names
    }


def _metrics_payload(
    *,
    snapshot: FrozenForwardDecisionSnapshot,
    evidence_status: EvidenceStatus,
    monthly: dict[str, Decimal | None],
    cumulative: dict[str, Decimal | None],
    statuses: dict[str, str],
) -> dict[str, Any]:
    candidate_cum = cumulative["candidate"]
    primary_cum = cumulative["primary"]
    us60_cum = cumulative["us60"]
    product_cum = cumulative["product_v1"]
    return {
        "report_month": snapshot.report_month,
        "observation_index": snapshot.observation_index,
        "observation_window": snapshot.observation_window,
        "evidence_status": evidence_status,
        "expected_git_main": snapshot.expected_git_main,
        "candidate_allocator_version": snapshot.candidate_allocator_version,
        "candidate_state_policy": snapshot.candidate_state_policy,
        "primary_benchmark": snapshot.primary_benchmark,
        "implemented_us60_static_policy": snapshot.implemented_us60_static_policy,
        "product_relative_v1_neutral_policy": snapshot.product_relative_v1_neutral_policy,
        "candidate_monthly_return": _json_decimal(monthly["candidate"]),
        "primary_benchmark_monthly_return": _json_decimal(monthly["primary"]),
        "implemented_us60_static_monthly_return": _json_decimal(monthly["us60"]),
        "product_relative_v1_neutral_monthly_return": _json_decimal(monthly["product_v1"]),
        "candidate_cumulative_return_to_date": _json_decimal(candidate_cum),
        "primary_benchmark_cumulative_return_to_date": _json_decimal(primary_cum),
        "implemented_us60_static_cumulative_return_to_date": _json_decimal(us60_cum),
        "product_relative_v1_neutral_cumulative_return_to_date": _json_decimal(product_cum),
        "candidate_minus_primary_benchmark_to_date": _json_difference(candidate_cum, primary_cum),
        "candidate_minus_us60_static_to_date": _json_difference(candidate_cum, us60_cum),
        "candidate_minus_product_relative_v1_to_date": _json_difference(
            candidate_cum,
            product_cum,
        ),
        **statuses,
    }


def _verify_frozen_identity(
    snapshot: FrozenForwardDecisionSnapshot,
    *,
    expected_git_main: str,
    candidate_allocator_version: str,
) -> None:
    expected = {
        "expected_git_main": expected_git_main,
        "candidate_allocator_version": candidate_allocator_version,
        "candidate_state_policy": FORWARD_CANDIDATE_STATE_POLICY,
        "primary_benchmark": FORWARD_PRIMARY_BENCHMARK,
        "implemented_us60_static_policy": FORWARD_IMPLEMENTED_US60_POLICY,
        "product_relative_v1_neutral_policy": FORWARD_PRODUCT_RELATIVE_V1_POLICY,
        "cost_model_version": FORWARD_COST_MODEL_VERSION,
        "observation_window": FORWARD_OBSERVATION_WINDOW,
    }
    for field, expected_value in expected.items():
        if getattr(snapshot, field) != expected_value:
            raise ForwardSnapshotIntegrityError(f"frozen identity changed: {field}.")


def _validate_expected_identity(
    *,
    repo_root: Path,
    expected_git_main: str,
    candidate_allocator_version: str,
    observed_git_head: str | None,
) -> str:
    expected = expected_git_main.strip().lower()
    if len(expected) != 40:
        raise ForwardObservationError("expected_git_main must be a 40-character SHA.")
    try:
        int(expected, 16)
    except ValueError as exc:
        raise ForwardObservationError("expected_git_main must be hexadecimal.") from exc
    if candidate_allocator_version != FORWARD_CANDIDATE_ALLOCATOR_VERSION:
        raise ForwardObservationError("candidate allocator version changed.")
    observed = (observed_git_head or _read_git_head(repo_root)).strip().lower()
    if observed != expected:
        raise ForwardObservationError("local HEAD does not match expected GitHub main.")
    return expected


def _read_git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ForwardObservationError("unable to resolve local repository HEAD.")
    return completed.stdout.strip()


def _validated_external_roots(*, repo_root: Path, output_root: Path) -> tuple[Path, Path]:
    resolved_repo = repo_root.resolve()
    resolved_output = output_root.resolve()
    if not resolved_repo.is_dir():
        raise ForwardObservationError("repo_root must be an existing directory.")
    if _is_relative_to(resolved_output, resolved_repo):
        raise ForwardObservationError("observation output root must be outside repo_root.")
    if _is_relative_to(resolved_repo, resolved_output):
        raise ForwardObservationError("repo_root must not be inside observation output root.")
    resolved_output.mkdir(parents=True, exist_ok=True)
    return resolved_repo, resolved_output


def _asset_period_values(
    dataset: LocalMonthlyDatasetAssemblyResult,
) -> dict[tuple[str, str], Decimal]:
    asset_by_symbol_market = {
        (spec.symbol, spec.market): spec.asset_id for spec in dataset.instrument_specs
    }
    selected: dict[tuple[str, str], tuple[Any, Decimal]] = {}
    for record in dataset.source_records:
        asset_id = asset_by_symbol_market.get((record.symbol, record.market))
        if asset_id is None:
            continue
        period = _record_period(record.payload)
        value = _decimal(record.payload.get("close_adjusted"), field_name="close_adjusted")
        if value <= _ZERO:
            raise ForwardDataQualityError("monthly adjusted close must be positive.")
        key = (asset_id, period)
        prior = selected.get(key)
        if prior is None or record.source_timestamp > prior[0]:
            selected[key] = (record.source_timestamp, value)
    return {key: item[1] for key, item in selected.items()}


def _decision_input_digest(
    dataset: LocalMonthlyDatasetAssemblyResult,
    *,
    cutoff: str,
) -> str:
    values = _asset_period_values(dataset)
    fx = {point.period_key: point.usdkrw_rate for point in dataset.fx_points}
    payload = {
        "common_periods": [period for period in dataset.common_periods if period <= cutoff],
        "asset_values": [
            {
                "asset_id": asset_id,
                "period": period,
                "value": _json_decimal(value),
            }
            for (asset_id, period), value in sorted(values.items())
            if period <= cutoff
        ],
        "fx_values": [
            {"period": period, "value": _json_decimal(value)}
            for period, value in sorted(fx.items())
            if period <= cutoff
        ],
    }
    return _payload_sha256(payload)


def _snapshot_payload(snapshot: FrozenForwardDecisionSnapshot) -> dict[str, Any]:
    return snapshot.model_dump(mode="json")


def _write_external_json(
    *,
    path: Path,
    payload: dict[str, Any],
    repo_root: Path,
    overwrite: bool,
    overwrite_deadline: date,
    today: date,
) -> None:
    resolved = path.resolve()
    if _is_relative_to(resolved, repo_root.resolve()):
        raise ForwardObservationError("generated artifacts must remain outside repo_root.")
    if resolved.exists():
        if not overwrite:
            raise ForwardObservationError("refusing to overwrite existing observation artifact.")
        if today >= overwrite_deadline:
            raise ForwardObservationError("safe overwrite deadline has passed.")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(resolved)


def _load_verified_json_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardDataQualityError("previous cumulative ledger is invalid.") from exc
    if not isinstance(document, dict):
        raise ForwardDataQualityError("previous cumulative ledger is invalid.")
    integrity = document.pop("integrity", None)
    if not isinstance(integrity, dict):
        raise ForwardDataQualityError("previous cumulative ledger integrity is missing.")
    if integrity.get("policy") != FORWARD_SNAPSHOT_INTEGRITY_POLICY_V1:
        raise ForwardDataQualityError("previous cumulative ledger integrity changed.")
    if integrity.get("sha256") != _payload_sha256(document):
        raise ForwardDataQualityError("previous cumulative ledger digest mismatch.")
    return document


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitized_weights(
    weights: tuple[tuple[str, Decimal], ...],
) -> tuple[SanitizedTargetWeight, ...]:
    return tuple(
        SanitizedTargetWeight(asset_id=asset_id, weight=weight)
        for asset_id, weight in weights
    )


def _validate_weights(
    weights: tuple[SanitizedTargetWeight, ...],
    *,
    label: str,
) -> None:
    asset_ids = tuple(item.asset_id for item in weights)
    if set(asset_ids) != {*_REQUIRED_ASSET_IDS, "cash"}:
        raise ValueError(f"{label} weights must contain the frozen asset set.")
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError(f"{label} weights must have unique asset ids.")
    if sum((item.weight for item in weights), _ZERO) != _ONE:
        raise ValueError(f"{label} weights must sum to 1.")


def _weight_signature(
    weights: tuple[SanitizedTargetWeight, ...],
) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted((item.asset_id, item.weight) for item in weights))


def _record_period(payload: dict[str, Any]) -> str:
    raw = payload.get("date")
    if not isinstance(raw, str):
        raise ForwardDataQualityError("monthly source date is invalid.")
    try:
        parsed = date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ForwardDataQualityError("monthly source date is invalid.") from exc
    return f"{parsed.year:04d}-{parsed.month:02d}"


def _parse_period(value: str) -> tuple[int, int]:
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError) as exc:
        raise ForwardObservationError("monthly period must use YYYY-MM.") from exc
    if value != f"{parsed.year:04d}-{parsed.month:02d}":
        raise ForwardObservationError("monthly period must use YYYY-MM.")
    return parsed.year, parsed.month


def _period_ordinal(value: str) -> int:
    year, month = _parse_period(value)
    return year * 12 + month - 1


def _period_from_ordinal(value: int) -> str:
    year, zero_month = divmod(value, 12)
    return f"{year:04d}-{zero_month + 1:02d}"


def _previous_period(value: str) -> str:
    return _period_from_ordinal(_period_ordinal(value) - 1)


def _observation_index(report_month: str) -> int:
    index = _period_ordinal(report_month) - _period_ordinal(FORWARD_OBSERVATION_START) + 1
    if index < 1 or index > FORWARD_MINIMUM_OBSERVATIONS:
        raise ForwardObservationError("report_month is outside the frozen observation window.")
    return index


def _period_start(value: str) -> date:
    year, month = _parse_period(value)
    return date(year, month, 1)


def _next_period_start(value: str) -> date:
    return _period_start(_period_from_ordinal(_period_ordinal(value) + 1))


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{field_name} must not be a float.")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a Decimal.") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return parsed


def _json_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _json_difference(left: Decimal | None, right: Decimal | None) -> str | None:
    if left is None or right is None:
        return None
    return str(left - right)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
