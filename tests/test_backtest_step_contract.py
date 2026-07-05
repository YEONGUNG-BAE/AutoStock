from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine import (  # noqa: E402
    RULES_ALLOCATOR_V1,
    BacktestAssetFeature,
    BacktestFeatureSnapshot,
    BacktestTargetWeight,
    BacktestTargetWeights,
)

DECISION_TIME = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
AS_OF = DECISION_TIME - timedelta(hours=1)
NAIVE_TIME = datetime(2026, 1, 31, 9, 0)


def _asset(**overrides: object) -> BacktestAssetFeature:
    base = {
        "asset_id": "asset_A",
        "as_of": AS_OF,
        "current_price": Decimal("110"),
        "long_ma": Decimal("100"),
        "risk_on_weight": Decimal("0.70"),
        "risk_off_weight": Decimal("0.35"),
        "min_weight": Decimal("0"),
        "max_weight": Decimal("0.80"),
    }
    base.update(overrides)
    return BacktestAssetFeature(**base)


def _snapshot(**overrides: object) -> BacktestFeatureSnapshot:
    base = {
        "decision_time": DECISION_TIME,
        "assets": (_asset(),),
        "cash_asset_id": "cash",
        "cash_min_weight": Decimal("0.05"),
    }
    base.update(overrides)
    return BacktestFeatureSnapshot(**base)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.decision_time = DECISION_TIME + timedelta(days=1)  # type: ignore[misc]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BacktestAssetFeature(
            asset_id="asset_A",
            as_of=AS_OF,
            current_price=Decimal("110"),
            long_ma=Decimal("100"),
            risk_on_weight=Decimal("0.70"),
            risk_off_weight=Decimal("0.35"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.80"),
            market="not_allowed",
        )


def test_naive_decision_time_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _snapshot(decision_time=NAIVE_TIME)


def test_naive_asset_as_of_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _asset(as_of=NAIVE_TIME)


def test_asset_as_of_after_decision_time_is_rejected() -> None:
    with pytest.raises(ValidationError, match="asset.as_of must be <= decision_time"):
        _snapshot(assets=(_asset(as_of=DECISION_TIME + timedelta(seconds=1)),))


@pytest.mark.parametrize(
    "field_name",
    (
        "current_price",
        "long_ma",
        "risk_on_weight",
        "risk_off_weight",
        "min_weight",
        "max_weight",
    ),
)
def test_asset_decimal_fields_reject_floats(field_name: str) -> None:
    with pytest.raises(ValidationError, match="floats are not accepted"):
        _asset(**{field_name: 0.5})


def test_snapshot_cash_min_weight_rejects_float() -> None:
    with pytest.raises(ValidationError, match="floats are not accepted"):
        _snapshot(cash_min_weight=0.05)


def test_target_weight_rejects_float() -> None:
    with pytest.raises(ValidationError, match="floats are not accepted"):
        BacktestTargetWeight(asset_id="asset_A", weight=0.5)


def test_non_positive_current_price_is_rejected() -> None:
    with pytest.raises(ValidationError, match="current_price must be greater than 0"):
        _asset(current_price=Decimal("0"))


def test_non_positive_long_ma_is_rejected() -> None:
    with pytest.raises(ValidationError, match="long_ma must be greater than 0"):
        _asset(long_ma=Decimal("0"))


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("risk_on_weight", Decimal("-0.01")),
        ("risk_off_weight", Decimal("1.01")),
        ("min_weight", Decimal("-0.01")),
        ("max_weight", Decimal("1.01")),
    ),
)
def test_weight_fields_outside_unit_interval_are_rejected(
    field_name: str, value: Decimal
) -> None:
    with pytest.raises(ValidationError, match="between 0 and 1 inclusive"):
        _asset(**{field_name: value})


def test_min_weight_above_max_weight_is_rejected() -> None:
    with pytest.raises(ValidationError, match="min_weight must be <= max_weight"):
        _asset(min_weight=Decimal("0.70"), max_weight=Decimal("0.60"))


def test_duplicate_asset_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="asset_id values must be unique"):
        _snapshot(assets=(_asset(asset_id="asset_A"), _asset(asset_id="asset_A")))


def test_asset_id_equal_to_cash_asset_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cash_asset_id must not equal any asset_id"):
        _snapshot(cash_asset_id="asset_A")


def test_min_weights_plus_cash_floor_above_one_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match=r"sum\(asset.min_weight\) \+ cash_min_weight must be <= 1"
    ):
        _snapshot(
            assets=(
                _asset(asset_id="asset_A", min_weight=Decimal("0.60")),
                _asset(asset_id="asset_B", min_weight=Decimal("0.36")),
            ),
            cash_min_weight=Decimal("0.05"),
        )


def test_generic_asset_ids_work_without_required_market_fields() -> None:
    snapshot = _snapshot(
        assets=(
            _asset(asset_id="asset_A"),
            _asset(asset_id="asset_B", risk_on_weight=Decimal("0.20")),
        )
    )
    assert tuple(asset.asset_id for asset in snapshot.assets) == ("asset_A", "asset_B")
    assert not hasattr(snapshot.assets[0], "kr")
    assert not hasattr(snapshot.assets[0], "us")
    assert not hasattr(snapshot.assets[0], "gold")


def test_target_weights_reject_total_outside_tolerance() -> None:
    with pytest.raises(ValidationError, match="total weight must equal 1"):
        BacktestTargetWeights(
            decision_time=DECISION_TIME,
            allocator_version=RULES_ALLOCATOR_V1,
            weights=(
                BacktestTargetWeight(asset_id="asset_A", weight=Decimal("0.50")),
                BacktestTargetWeight(asset_id="cash", weight=Decimal("0.49999998")),
            ),
        )


def test_target_weights_accept_total_within_tolerance() -> None:
    target = BacktestTargetWeights(
        decision_time=DECISION_TIME,
        allocator_version=RULES_ALLOCATOR_V1,
        weights=(
            BacktestTargetWeight(asset_id="asset_A", weight=Decimal("0.50")),
            BacktestTargetWeight(asset_id="cash", weight=Decimal("0.499999995")),
        ),
    )
    assert target.allocator_version == "rules_allocator.v1"
