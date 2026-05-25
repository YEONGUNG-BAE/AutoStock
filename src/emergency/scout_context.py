from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from domain.identifiers import DateId
from emergency.models import EmergencyTriggerType, TriggerPayload, trigger_priority_rank


class EmergencyScoutContext(BaseModel):
    """긴급 Scout/Analysis 호출용 컨텍스트. Phase 15에서는 LLM을 호출하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_payload: TriggerPayload
    scope_symbols: tuple[str, ...]
    market: Market | None
    reason: str
    priority: int
    required_focus: str
    date_ids_used: tuple[DateId, ...] = ()
    portfolio_snapshot_hash: str
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("portfolio_snapshot_hash", mode="before")
    @classmethod
    def validate_hash(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("portfolio_snapshot_hash must not be blank.")
        return value.strip()

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic canonical dict 표현."""
        payload: dict[str, Any] = {
            "trigger_payload": self.trigger_payload.to_canonical_dict(),
            "scope_symbols": list(self.scope_symbols),
            "market": self.market.value if self.market is not None else None,
            "reason": self.reason,
            "priority": self.priority,
            "required_focus": self.required_focus,
            "date_ids_used": [item.value for item in self.date_ids_used],
            "portfolio_snapshot_hash": self.portfolio_snapshot_hash,
            "created_at": self.created_at.isoformat(),
        }
        return canonicalize_payload(payload)


_FOCUS_BY_TYPE: dict[EmergencyTriggerType, str] = {
    EmergencyTriggerType.STOCK_DROP: "held_stock_and_same_sector_damage",
    EmergencyTriggerType.INDEX_CRASH: "market_wide_holdings_in_affected_market",
    EmergencyTriggerType.PORTFOLIO_LOSS: "top_loss_contributors",
    EmergencyTriggerType.PROFIT_RUN: "single_name_concentration_review",
    EmergencyTriggerType.MDD_KILLSWITCH: "python_only_no_llm",
}


_REASON_BY_TYPE: dict[EmergencyTriggerType, str] = {
    EmergencyTriggerType.STOCK_DROP: "Held stock intraday drop exceeded threshold",
    EmergencyTriggerType.INDEX_CRASH: "Market index intraday crash exceeded threshold",
    EmergencyTriggerType.PORTFOLIO_LOSS: "Portfolio intraday loss exceeded threshold",
    EmergencyTriggerType.PROFIT_RUN: "Single-stock market weight reached staged threshold",
    EmergencyTriggerType.MDD_KILLSWITCH: "Account MDD killswitch stage reached (Python-only)",
}


def build_emergency_scout_context(
    *,
    trigger_payload: TriggerPayload,
    portfolio_snapshot_hash: str,
    created_at: datetime,
    date_ids_used: tuple[DateId, ...] = (),
) -> EmergencyScoutContext | None:
    """TriggerPayload에서 EmergencyScoutContext를 생성한다.

    MDD_KILLSWITCH와 PROFIT_RUN 10% monitoring(NOOP)은 LLM review가 필요 없으므로 None.
    """
    if trigger_payload.trigger_type == EmergencyTriggerType.MDD_KILLSWITCH:
        return None

    if not trigger_payload.requires_llm_review:
        return None

    return EmergencyScoutContext(
        trigger_payload=trigger_payload,
        scope_symbols=trigger_payload.scope_symbols,
        market=trigger_payload.market,
        reason=_REASON_BY_TYPE[trigger_payload.trigger_type],
        priority=trigger_priority_rank(trigger_payload.trigger_type),
        required_focus=_FOCUS_BY_TYPE[trigger_payload.trigger_type],
        date_ids_used=date_ids_used,
        portfolio_snapshot_hash=portfolio_snapshot_hash,
        created_at=created_at,
    )


def compute_portfolio_snapshot_hash(snapshot: dict[str, Any]) -> str:
    """포트폴리오 스냅샷 dict의 deterministic hash."""
    return payload_sha256(snapshot)
