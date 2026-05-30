from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from domain.source import DateIdSourceRecord, FactType

ContextBudgetProfileName = Literal["kr-real-smoke"]


@dataclass(frozen=True)
class ContextBudgetCaps:
    """Date.md / Scout export용 deterministic context cap 설정.

    None 값은 해당 그룹에 cap을 적용하지 않음을 의미한다.
    store record는 변경하지 않고 export 선택에만 사용한다.
    """

    max_global_per_fact_type_source: int | None = None
    max_price_per_symbol_source: int | None = None
    max_disclosure_per_symbol_source: int | None = None

    def is_active(self) -> bool:
        return (
            self.max_global_per_fact_type_source is not None
            or self.max_price_per_symbol_source is not None
            or self.max_disclosure_per_symbol_source is not None
        )


KR_REAL_SMOKE_CONTEXT_BUDGET = ContextBudgetCaps(
    max_global_per_fact_type_source=5,
    max_price_per_symbol_source=1,
    max_disclosure_per_symbol_source=5,
)


def resolve_context_budget_caps(
    *,
    profile: ContextBudgetProfileName | None,
    max_global_per_fact_type_source: int | None = None,
    max_price_per_symbol_source: int | None = None,
    max_disclosure_per_symbol_source: int | None = None,
) -> ContextBudgetCaps | None:
    """CLI profile/명시 flag를 ContextBudgetCaps로 해석한다. cap이 없으면 None."""
    explicit_any = (
        max_global_per_fact_type_source is not None
        or max_price_per_symbol_source is not None
        or max_disclosure_per_symbol_source is not None
    )
    if profile is None and not explicit_any:
        return None

    if profile == "kr-real-smoke":
        caps = KR_REAL_SMOKE_CONTEXT_BUDGET
    else:
        caps = ContextBudgetCaps()

    if max_global_per_fact_type_source is not None:
        caps = replace(caps, max_global_per_fact_type_source=max_global_per_fact_type_source)
    if max_price_per_symbol_source is not None:
        caps = replace(caps, max_price_per_symbol_source=max_price_per_symbol_source)
    if max_disclosure_per_symbol_source is not None:
        caps = replace(caps, max_disclosure_per_symbol_source=max_disclosure_per_symbol_source)

    return caps if caps.is_active() else None


def select_context_records(
    records: Sequence[DateIdSourceRecord],
    *,
    caps: ContextBudgetCaps,
) -> tuple[DateIdSourceRecord, ...]:
    """store record 목록에서 export/context용 record subset을 deterministic하게 선택한다."""
    if not caps.is_active():
        return _sort_for_export(records)

    global_records: list[DateIdSourceRecord] = []
    price_records: list[DateIdSourceRecord] = []
    disclosure_records: list[DateIdSourceRecord] = []
    other_records: list[DateIdSourceRecord] = []

    for record in records:
        if record.symbol is None and record.market is None:
            global_records.append(record)
            continue
        if (
            record.fact_type == FactType.PRICE
            and record.symbol is not None
            and record.market is not None
        ):
            price_records.append(record)
            continue
        if (
            record.fact_type == FactType.DISCLOSURE
            and record.symbol is not None
            and record.market is None
        ):
            disclosure_records.append(record)
            continue
        other_records.append(record)

    selected: list[DateIdSourceRecord] = []
    selected.extend(
        _cap_grouped_records(
            global_records,
            group_key=lambda record: (record.fact_type, record.source_name),
            max_per_group=caps.max_global_per_fact_type_source,
        )
    )
    selected.extend(
        _cap_grouped_records(
            price_records,
            group_key=lambda record: (record.market, record.symbol, record.source_name),
            max_per_group=caps.max_price_per_symbol_source,
        )
    )
    selected.extend(
        _cap_grouped_records(
            disclosure_records,
            group_key=lambda record: (record.symbol, record.source_name),
            max_per_group=caps.max_disclosure_per_symbol_source,
        )
    )
    selected.extend(other_records)
    return _sort_for_export(selected)


def _cap_grouped_records(
    records: Sequence[DateIdSourceRecord],
    *,
    group_key,
    max_per_group: int | None,
) -> list[DateIdSourceRecord]:
    if not records:
        return []
    if max_per_group is None:
        return list(records)
    if max_per_group <= 0:
        raise ValueError("context budget cap must be a positive integer")

    grouped: dict[tuple[object, ...], list[DateIdSourceRecord]] = defaultdict(list)
    for record in records:
        grouped[group_key(record)].append(record)

    selected: list[DateIdSourceRecord] = []
    for group_records in grouped.values():
        selected.extend(_select_top_n(group_records, max_per_group))
    return selected


def _select_top_n(
    records: Sequence[DateIdSourceRecord],
    limit: int,
) -> list[DateIdSourceRecord]:
    ranked = sorted(
        records,
        key=lambda record: (-record.source_timestamp.timestamp(), record.date_id.value),
    )
    return ranked[:limit]


def _sort_for_export(records: Sequence[DateIdSourceRecord]) -> tuple[DateIdSourceRecord, ...]:
    return tuple(sorted(records, key=lambda record: record.date_id.value))
