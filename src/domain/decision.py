from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId, DecisionId
from domain.validation import ValidationResult


class EvidenceRef(BaseModel):
    """LLM 판단 근거가 어떤 Date-ID에 연결되는지 표현한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    date_id: DateId
    source_name: str | None = None
    source_url: str | None = None
    quote: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="reason")

    @field_validator("source_name", "source_url", "quote", mode="before")
    @classmethod
    def validate_optional_strings(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)


class DecisionSnapshot(BaseModel):
    """LLM raw output, normalized payload, validation result, replay metadata를 저장한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: DecisionId
    created_at: datetime
    schema_name: str
    raw_payload: dict[str, Any]
    normalized_payload: dict[str, Any]
    payload_hash: str
    validation_result: ValidationResult
    order_intent_ids: tuple[str, ...] = ()
    replay_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_name", mode="before")
    @classmethod
    def validate_schema_name(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="schema_name")

    @field_validator("payload_hash", mode="before")
    @classmethod
    def validate_payload_hash(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="payload_hash")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("order_intent_ids", mode="before")
    @classmethod
    def validate_order_intent_ids(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("order_intent_ids must be a sequence of strings.")

        normalized_ids: list[str] = []
        for index, item in enumerate(value):
            normalized_ids.append(
                normalize_required_string(item, field_name=f"order_intent_ids[{index}]")
            )
        return tuple(normalized_ids)

    @field_validator("raw_payload", "normalized_payload", "replay_metadata", mode="before")
    @classmethod
    def validate_json_compatible_object(cls, value: Any, info) -> dict[str, Any]:
        from decision.canonical_json import canonicalize_payload

        if not isinstance(value, dict):
            raise ValueError(f"{info.field_name} must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_snapshot_consistency(self) -> Self:
        from decision.canonical_json import canonicalize_payload, payload_sha256

        expected_normalized = canonicalize_payload(self.raw_payload)
        if self.normalized_payload != expected_normalized:
            raise ValueError("normalized_payload must equal canonicalize_payload(raw_payload).")

        expected_hash = payload_sha256(self.normalized_payload)
        if self.payload_hash != expected_hash:
            raise ValueError("payload_hash must equal payload_sha256(normalized_payload).")

        return self

    @classmethod
    def create(
        cls,
        *,
        decision_id: DecisionId,
        created_at: datetime,
        schema_name: str,
        raw_payload: dict[str, Any],
        validation_result: ValidationResult,
        order_intent_ids: tuple[str, ...] = (),
        replay_metadata: dict[str, Any] | None = None,
    ) -> DecisionSnapshot:
        """raw_payload를 canonicalize하여 normalized_payload와 payload_hash를 생성한다."""
        from decision.canonical_json import canonicalize_payload, payload_sha256

        normalized_payload = canonicalize_payload(raw_payload)
        metadata = {} if replay_metadata is None else canonicalize_payload(replay_metadata)
        return cls(
            decision_id=decision_id,
            created_at=created_at,
            schema_name=schema_name,
            raw_payload=raw_payload,
            normalized_payload=normalized_payload,
            payload_hash=payload_sha256(normalized_payload),
            validation_result=validation_result,
            order_intent_ids=order_intent_ids,
            replay_metadata=metadata,
        )
