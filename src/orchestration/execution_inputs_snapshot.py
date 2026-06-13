"""RTM-7c.4a — validated, restart-restorable fast-loop execution inputs snapshot.

`StaticExecutionInputsProvider`(테스트/오프라인 고정 객체)와 달리, 이 모듈은 운영자가
디스크에 둔 **구조화 JSON**을 fail-closed로 검증해 immutable snapshot으로 적재하고, 매 tick
재로딩 없이 동일 snapshot을 재사용하는 runtime `ExecutionInputsProvider`를 제공한다.

핵심 계약:
  - schema_version == 1, unknown 필드 거부, bool/int coercion 거부, non-finite Decimal 거부.
  - payload_sha256 = (hash 필드를 제외한) 전체 canonical payload의 SHA-256. 변조 → fail-closed.
  - created_at/expires_at tz-aware, created_at <= expires_at.
  - allocator_decision.universe == universe, allocator_decision.created_at <= created_at.
  - raw LLM output/prose/markdown에서 값을 추론하지 않는다(구조화 입력만).
  - 로더 실패 reason에 raw JSON/예외 원문/파일 전체 payload를 노출하지 않는다.

network/broker/ledger/data adapter/LLM import이 없다. PaperPortfolioPolicy는 실행 입력의
일부이므로 `execution.paper_portfolio_context`에서만 가져온다(allocator/domain/risk 동일).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from allocator.models import AllocatorDecision, AssetBucket
from decision.canonical_json import canonicalize_payload, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import Percent

from execution.paper_portfolio_context import PaperPortfolioPolicy
from risk.models import RiskMode

from orchestration.active_decision_store import ActiveBundle
from orchestration.fast_loop_execution import ExecutionInputs

__all__ = [
    "EXECUTION_INPUTS_SNAPSHOT_SCHEMA_VERSION",
    "ExecutionInputsSnapshotError",
    "ValidatedExecutionInputsProvider",
    "ValidatedExecutionInputsSnapshot",
    "compute_snapshot_payload_hash",
    "load_execution_inputs_snapshot",
]

EXECUTION_INPUTS_SNAPSHOT_SCHEMA_VERSION = 1

_HASH_FIELD = "payload_sha256"
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "source_id",
        "created_at",
        "expires_at",
        "universe",
        "allocator_decision",
        "portfolio_policy",
        _HASH_FIELD,
    }
)
_POLICY_KEYS = frozenset(
    {
        "mode",
        "allocator_tolerance_percent",
        "allocator_symbol_target_weight",
        "paper_observation_min_invested_percent",
        "mdd_percent",
        "gold_trades_this_month",
        "gold_trades_this_quarter",
        "asset_bucket",
        "metadata",
    }
)
_PERCENT_FIELDS = (
    "allocator_tolerance_percent",
    "allocator_symbol_target_weight",
    "paper_observation_min_invested_percent",
    "mdd_percent",
)


class ExecutionInputsSnapshotError(Exception):
    """snapshot 로드/검증/해석 실패. reason_code만 노출하고 raw payload는 노출하지 않는다."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ValidatedExecutionInputsSnapshot:
    """검증을 통과한 immutable 실행 입력 snapshot. 생성 후 변이 불가(frozen)."""

    schema_version: int
    source_id: str
    created_at: datetime
    expires_at: datetime
    universe: str
    allocator_decision: AllocatorDecision
    portfolio_policy: PaperPortfolioPolicy
    payload_sha256: str


def compute_snapshot_payload_hash(payload: dict[str, Any]) -> str:
    """hash 필드를 제외한 payload의 canonical SHA-256를 계산한다(fixture/검증 공용)."""
    without_hash = {key: value for key, value in payload.items() if key != _HASH_FIELD}
    return payload_sha256(without_hash)


def load_execution_inputs_snapshot(path: Path | str) -> ValidatedExecutionInputsSnapshot:
    """UTF-8 JSON snapshot 파일을 fail-closed로 검증해 immutable snapshot으로 적재한다."""
    import json

    file_path = Path(path)
    try:
        raw_bytes = file_path.read_bytes()
    except FileNotFoundError as exc:
        raise ExecutionInputsSnapshotError(
            "snapshot_file_missing", "execution inputs snapshot file not found."
        ) from exc
    except OSError as exc:
        raise ExecutionInputsSnapshotError(
            "snapshot_file_unreadable", "execution inputs snapshot file unreadable."
        ) from exc

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutionInputsSnapshotError(
            "snapshot_not_utf8", "execution inputs snapshot is not valid UTF-8."
        ) from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExecutionInputsSnapshotError(
            "snapshot_not_json", "execution inputs snapshot is not valid JSON."
        ) from exc

    return _validate_payload(payload)


def _validate_payload(payload: Any) -> ValidatedExecutionInputsSnapshot:
    if not isinstance(payload, dict):
        raise ExecutionInputsSnapshotError(
            "snapshot_not_object", "execution inputs snapshot must be a JSON object."
        )
    keys = set(payload)
    unknown = keys - _TOP_LEVEL_KEYS
    if unknown:
        raise ExecutionInputsSnapshotError(
            "snapshot_unknown_field", "execution inputs snapshot has unknown top-level fields."
        )
    missing = _TOP_LEVEL_KEYS - keys
    if missing:
        raise ExecutionInputsSnapshotError(
            "snapshot_missing_field", "execution inputs snapshot is missing required fields."
        )

    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_field", "schema_version must be an int."
        )
    if schema_version != EXECUTION_INPUTS_SNAPSHOT_SCHEMA_VERSION:
        raise ExecutionInputsSnapshotError(
            "snapshot_unsupported_version", "unsupported execution inputs snapshot schema_version."
        )

    stored_hash = payload[_HASH_FIELD]
    if not isinstance(stored_hash, str) or not stored_hash.strip():
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_field", "payload_sha256 must be a nonblank string."
        )
    try:
        recomputed = compute_snapshot_payload_hash(payload)
    except Exception as exc:  # noqa: BLE001 - fail-closed, no raw leak
        raise ExecutionInputsSnapshotError(
            "snapshot_uncanonicalizable", "execution inputs snapshot could not be canonicalized."
        ) from exc
    if recomputed != stored_hash:
        raise ExecutionInputsSnapshotError(
            "snapshot_hash_mismatch", "execution inputs snapshot payload hash mismatch."
        )

    source_id = payload["source_id"]
    if not isinstance(source_id, str) or not source_id.strip():
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_field", "source_id must be a nonblank string."
        )

    universe = payload["universe"]
    if not isinstance(universe, str) or not universe.strip():
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_field", "universe must be a nonblank string."
        )

    created_at = _parse_datetime(payload["created_at"], field="created_at")
    expires_at = _parse_datetime(payload["expires_at"], field="expires_at")
    if created_at > expires_at:
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_validity", "created_at must be <= expires_at."
        )

    allocator_decision = _parse_allocator(payload["allocator_decision"])
    if allocator_decision.universe != universe:
        raise ExecutionInputsSnapshotError(
            "snapshot_universe_mismatch", "allocator_decision.universe must equal snapshot universe."
        )
    if allocator_decision.created_at > created_at:
        raise ExecutionInputsSnapshotError(
            "snapshot_allocator_created_after",
            "allocator_decision.created_at must be <= snapshot created_at.",
        )

    portfolio_policy = _parse_policy(payload["portfolio_policy"])

    return ValidatedExecutionInputsSnapshot(
        schema_version=schema_version,
        source_id=source_id,
        created_at=created_at,
        expires_at=expires_at,
        universe=universe,
        allocator_decision=allocator_decision,
        portfolio_policy=portfolio_policy,
        payload_sha256=stored_hash,
    )


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_field", f"{field} must be an ISO-8601 string."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_field", f"{field} is not a valid ISO-8601 datetime."
        ) from exc
    try:
        return require_timezone_aware_datetime(parsed, field_name=field)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        raise ExecutionInputsSnapshotError(
            "snapshot_naive_datetime", f"{field} must be timezone-aware."
        ) from exc


def _parse_allocator(value: Any) -> AllocatorDecision:
    if not isinstance(value, dict):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_allocator", "allocator_decision must be a JSON object."
        )
    try:
        return AllocatorDecision.model_validate(value)
    except Exception as exc:  # noqa: BLE001 - fail-closed, no raw leak
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_allocator", "allocator_decision failed schema validation."
        ) from exc


def _parse_policy(value: Any) -> PaperPortfolioPolicy:
    if not isinstance(value, dict):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy must be a JSON object."
        )
    unknown = set(value) - _POLICY_KEYS
    if unknown:
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy has unknown fields."
        )

    mode_value = value.get("mode")
    if isinstance(mode_value, bool) or not isinstance(mode_value, str):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy.mode must be a string."
        )
    try:
        mode = RiskMode(mode_value)
    except ValueError as exc:
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy.mode is not a valid RiskMode."
        ) from exc

    percents: dict[str, Percent | None] = {}
    for field in _PERCENT_FIELDS:
        percents[field] = _parse_optional_percent(value.get(field), field=field)
    # tolerance has a non-None default; keep it required-with-default behaviour explicit.
    tolerance = percents["allocator_tolerance_percent"]
    if tolerance is None:
        tolerance = Percent("5")

    gold_month = _parse_non_negative_int(
        value.get("gold_trades_this_month", 0), field="gold_trades_this_month"
    )
    gold_quarter = _parse_non_negative_int(
        value.get("gold_trades_this_quarter", 0), field="gold_trades_this_quarter"
    )

    bucket_value = value.get("asset_bucket")
    asset_bucket: AssetBucket | None
    if bucket_value is None:
        asset_bucket = None
    elif isinstance(bucket_value, bool) or not isinstance(bucket_value, str):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy.asset_bucket must be a string or null."
        )
    else:
        try:
            asset_bucket = AssetBucket(bucket_value)
        except ValueError as exc:
            raise ExecutionInputsSnapshotError(
                "snapshot_invalid_policy", "portfolio_policy.asset_bucket is not a valid AssetBucket."
            ) from exc

    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy.metadata must be a JSON object."
        )
    try:
        canonicalize_payload(metadata)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy.metadata must be canonical JSON."
        ) from exc

    try:
        return PaperPortfolioPolicy(
            mode=mode,
            allocator_tolerance_percent=tolerance,
            allocator_symbol_target_weight=percents["allocator_symbol_target_weight"],
            paper_observation_min_invested_percent=percents[
                "paper_observation_min_invested_percent"
            ],
            mdd_percent=percents["mdd_percent"],
            gold_trades_this_month=gold_month,
            gold_trades_this_quarter=gold_quarter,
            asset_bucket=asset_bucket,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", "portfolio_policy could not be constructed."
        ) from exc


def _parse_optional_percent(value: Any, *, field: str) -> Percent | None:
    if value is None:
        return None
    # bool/int/float coercion 거부: percent는 명시적 문자열로만 받는다(부동소수 비결정성 차단).
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", f"portfolio_policy.{field} must be a decimal string or null."
        )
    try:
        return Percent(value)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", f"portfolio_policy.{field} is not a valid percent."
        ) from exc


def _parse_non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", f"portfolio_policy.{field} must be an int."
        )
    if value < 0:
        raise ExecutionInputsSnapshotError(
            "snapshot_invalid_policy", f"portfolio_policy.{field} must be >= 0."
        )
    return value


@dataclass(frozen=True)
class ValidatedExecutionInputsProvider:
    """immutable snapshot을 보유하고 active bundle/now에 바인딩해 ExecutionInputs를 산출한다.

    DB/network/fs write를 하지 않으며 파일을 재로딩하지 않는다(composition 시 1회 적재).
    """

    snapshot: ValidatedExecutionInputsSnapshot

    def resolve(self, *, active: ActiveBundle, now: datetime) -> ExecutionInputs:
        require_timezone_aware_datetime(now, field_name="now")
        snap = self.snapshot
        if now < snap.created_at:
            raise ExecutionInputsSnapshotError(
                "snapshot_not_yet_valid", "snapshot.created_at is in the future relative to now."
            )
        if now > snap.expires_at:
            raise ExecutionInputsSnapshotError(
                "snapshot_expired", "snapshot has expired relative to now."
            )

        decision = active.bundle.decision
        if snap.universe != decision.universe:
            raise ExecutionInputsSnapshotError(
                "snapshot_active_universe_mismatch",
                "snapshot universe does not match active decision universe.",
            )
        alloc = snap.allocator_decision
        if alloc.universe != decision.universe:
            raise ExecutionInputsSnapshotError(
                "snapshot_active_universe_mismatch",
                "allocator universe does not match active decision universe.",
            )
        if alloc.created_at > now:
            raise ExecutionInputsSnapshotError(
                "snapshot_allocator_created_after", "allocator created_at is in the future."
            )
        plan = active.bundle.plan
        if plan is not None and plan.universe != decision.universe:
            raise ExecutionInputsSnapshotError(
                "snapshot_active_universe_mismatch",
                "active plan universe does not match active decision universe.",
            )
        return ExecutionInputs(
            allocator_decision=alloc,
            portfolio_policy=snap.portfolio_policy,
        )
