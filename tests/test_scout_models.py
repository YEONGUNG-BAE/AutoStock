from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scout import (
    SUMMARY_ONE_LINER_MAX_LENGTH,
    ScoutFactor,
    ScoutReason,
    ScoutSummary,
)
from domain import DateId, DecisionId, Percent


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_reason(**overrides: object) -> ScoutReason:
    base = {
        "reason": "VIX 상승으로 위험 회피 심리가 강화됐다.",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return ScoutReason(**base)


def _sample_factor(**overrides: object) -> ScoutFactor:
    base = {
        "name": "macro_risk",
        "summary": "매크로 리스크 요인",
        "reasons": (_sample_reason(),),
    }
    base.update(overrides)
    return ScoutFactor(**base)


def _sample_summary(**overrides: object) -> ScoutSummary:
    base = {
        "summary_id": DecisionId("scout-260522-001"),
        "created_at": NOW,
        "universe": "US",
        "summary_one_liner": "매크로 불확실성이 높아진 상태다.",
        "positive_factors": (_sample_factor(name="positive_signal"),),
        "negative_factors": (),
        "neutral_factors": (),
    }
    base.update(overrides)
    return ScoutSummary(**base)


def test_scout_reason_accepts_valid_values() -> None:
    reason = _sample_reason(source_name="fred", quote="VIX 18.2")
    assert reason.date_id.value == "260522-1"
    assert reason.source_name == "fred"
    assert reason.quote == "VIX 18.2"


def test_scout_reason_rejects_invalid_date_id() -> None:
    with pytest.raises(ValidationError, match="canonical format"):
        _sample_reason(date_id="bad-id")


def test_scout_reason_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_reason(reason=" ")


@pytest.mark.parametrize("field_name", ["source_name", "quote"])
def test_scout_reason_rejects_blank_optional_strings(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_reason(**{field_name: " "})


def test_scout_factor_accepts_valid_values() -> None:
    factor = _sample_factor(strength=Percent("72.5"))
    assert factor.name == "macro_risk"
    assert factor.strength is not None
    assert factor.strength.value == Decimal("72.5")


def test_scout_factor_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_factor(reasons=())


@pytest.mark.parametrize("field_name", ["name", "summary"])
def test_scout_factor_rejects_blank_required_fields(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_factor(**{field_name: " "})


def test_scout_summary_accepts_valid_values() -> None:
    summary = _sample_summary(
        negative_factors=(_sample_factor(name="negative_signal"),),
        neutral_factors=(_sample_factor(name="neutral_signal"),),
        metadata={"phase": 7},
    )
    assert summary.summary_id.value == "scout-260522-001"
    assert summary.universe == "US"
    assert len(summary.positive_factors) == 1


def test_scout_summary_rejects_blank_summary_one_liner() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_summary(summary_one_liner=" ")


def test_scout_summary_rejects_too_long_summary_one_liner() -> None:
    too_long = "x" * (SUMMARY_ONE_LINER_MAX_LENGTH + 1)
    with pytest.raises(ValidationError, match=f"at most {SUMMARY_ONE_LINER_MAX_LENGTH}"):
        _sample_summary(summary_one_liner=too_long)


def test_scout_summary_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _sample_summary(created_at=NAIVE_NOW)


def test_scout_summary_rejects_all_factor_groups_empty() -> None:
    with pytest.raises(ValidationError, match="at least one factor"):
        _sample_summary(positive_factors=(), negative_factors=(), neutral_factors=())


def test_scout_summary_rejects_invalid_metadata() -> None:
    with pytest.raises(ValidationError, match="float values are not allowed"):
        _sample_summary(metadata={"bad": 1.5})


def test_scout_summary_rejects_extra_trading_action_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScoutSummary.model_validate(
            {
                "summary_id": "scout-260522-001",
                "created_at": NOW.isoformat(),
                "universe": "US",
                "summary_one_liner": "test",
                "positive_factors": [
                    {
                        "name": "factor",
                        "summary": "summary",
                        "reasons": [{"reason": "r", "date_id": "260522-1"}],
                    }
                ],
                "action": "BUY",
            }
        )


def test_scout_summary_factor_category_ordering_preserved() -> None:
    positive = _sample_factor(name="pos")
    negative = _sample_factor(name="neg")
    neutral = _sample_factor(name="neu")
    summary = _sample_summary(
        positive_factors=(positive,),
        negative_factors=(negative,),
        neutral_factors=(neutral,),
    )
    assert summary.positive_factors[0].name == "pos"
    assert summary.negative_factors[0].name == "neg"
    assert summary.neutral_factors[0].name == "neu"


def test_scout_summary_to_canonical_dict_is_deterministic() -> None:
    summary = _sample_summary(metadata={"z": "3", "a": "1"})
    canonical = summary.to_canonical_dict()
    assert canonical["metadata"] == {"a": "1", "z": "3"}
    assert canonical["summary_id"] == "scout-260522-001"
