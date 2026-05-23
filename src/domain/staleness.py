from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

from domain._datetime import require_timezone_aware_datetime
from domain.source import DateIdSourceRecord, FactType

# fact_type별 기본 허용 stale 시간
_DEFAULT_MAX_AGES: dict[FactType, timedelta] = {
    FactType.PRICE: timedelta(hours=24),
    FactType.FLOW: timedelta(hours=24),
    FactType.FX: timedelta(hours=24),
    FactType.NEWS: timedelta(days=7),
    FactType.DISCLOSURE: timedelta(days=90),
    FactType.MACRO: timedelta(days=30),
    FactType.MANUAL: timedelta(days=365),
}


@dataclass(frozen=True)
class StalenessPolicy:
    """fact_type별 허용 stale 시간을 정의한다."""

    overrides: Mapping[FactType, timedelta] = field(default_factory=dict)

    def allowed_age_for(self, fact_type: FactType) -> timedelta:
        """fact_type에 허용되는 최대 age를 반환한다."""
        if fact_type in self.overrides:
            return self.overrides[fact_type]
        return _DEFAULT_MAX_AGES[fact_type]

    def age(self, record: DateIdSourceRecord, now: datetime) -> timedelta:
        """record.source_timestamp 기준 age를 계산한다."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        source_timestamp = record.source_timestamp
        if source_timestamp > aware_now:
            raise ValueError(
                f"source_timestamp must not be in the future: {source_timestamp.isoformat()}"
            )
        return aware_now - source_timestamp

    def is_stale(self, record: DateIdSourceRecord, *, now: datetime) -> bool:
        """age > allowed_age이면 stale, age <= allowed_age이면 fresh."""
        record_age = self.age(record, now)
        allowed_age = self.allowed_age_for(record.fact_type)
        return record_age > allowed_age
