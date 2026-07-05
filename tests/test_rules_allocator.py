from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine import (  # noqa: E402
    DECIMAL_WEIGHT_TOLERANCE,
    RULES_ALLOCATOR_V1,
    BacktestAssetFeature,
    BacktestFeatureSnapshot,
    allocate_rules_only_v1,
)

DECISION_TIME = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
AS_OF = DECISION_TIME - timedelta(hours=1)


def _asset(
    asset_id: str,
    *,
    current_price: Decimal = Decimal("110"),
    long_ma: Decimal = Decimal("100"),
    risk_on_weight: Decimal,
    risk_off_weight: Decimal,
    min_weight: Decimal,
    max_weight: Decimal,
) -> BacktestAssetFeature:
    return BacktestAssetFeature(
        asset_id=asset_id,
        as_of=AS_OF,
        current_price=current_price,
        long_ma=long_ma,
        risk_on_weight=risk_on_weight,
        risk_off_weight=risk_off_weight,
        min_weight=min_weight,
        max_weight=max_weight,
    )


def _fixture_asset(asset_id: str, *, risk_on: bool = True) -> BacktestAssetFeature:
    prices = {
        True: (Decimal("110"), Decimal("100")),
        False: (Decimal("90"), Decimal("100")),
    }
    current_price, long_ma = prices[risk_on]
    params = {
        "asset_A": {
            "risk_on_weight": Decimal("0.70"),
            "risk_off_weight": Decimal("0.35"),
            "min_weight": Decimal("0"),
            "max_weight": Decimal("0.80"),
        },
        "asset_B": {
            "risk_on_weight": Decimal("0.15"),
            "risk_off_weight": Decimal("0.05"),
            "min_weight": Decimal("0"),
            "max_weight": Decimal("0.25"),
        },
        "asset_C": {
            "risk_on_weight": Decimal("0.10"),
            "risk_off_weight": Decimal("0.10"),
            "min_weight": Decimal("0"),
            "max_weight": Decimal("0.20"),
        },
    }
    return _asset(asset_id, current_price=current_price, long_ma=long_ma, **params[asset_id])


def _snapshot(*assets: BacktestAssetFeature, cash_min_weight: Decimal = Decimal("0.05")):
    return BacktestFeatureSnapshot(
        decision_time=DECISION_TIME,
        assets=assets,
        cash_asset_id="cash",
        cash_min_weight=cash_min_weight,
    )


def _weights_by_id(snapshot: BacktestFeatureSnapshot) -> dict[str, Decimal]:
    return {
        target.asset_id: target.weight
        for target in allocate_rules_only_v1(snapshot).weights
    }


def test_deterministic_same_snapshot_returns_identical_target_weights() -> None:
    snapshot = _snapshot(
        _fixture_asset("asset_A"),
        _fixture_asset("asset_B"),
        _fixture_asset("asset_C"),
    )
    assert allocate_rules_only_v1(snapshot) == allocate_rules_only_v1(snapshot)


def test_all_risk_on_example_produces_expected_weights() -> None:
    snapshot = _snapshot(
        _fixture_asset("asset_A"),
        _fixture_asset("asset_B"),
        _fixture_asset("asset_C"),
    )
    assert _weights_by_id(snapshot) == {
        "asset_A": Decimal("0.70"),
        "asset_B": Decimal("0.15"),
        "asset_C": Decimal("0.10"),
        "cash": Decimal("0.05"),
    }


def test_mixed_risk_off_example_produces_expected_weights() -> None:
    snapshot = _snapshot(
        _fixture_asset("asset_A", risk_on=False),
        _fixture_asset("asset_B", risk_on=False),
        _fixture_asset("asset_C"),
    )
    assert _weights_by_id(snapshot) == {
        "asset_A": Decimal("0.35"),
        "asset_B": Decimal("0.05"),
        "asset_C": Decimal("0.10"),
        "cash": Decimal("0.50"),
    }


def test_current_price_equal_to_long_ma_is_risk_on() -> None:
    snapshot = _snapshot(
        _asset(
            "asset_A",
            current_price=Decimal("100"),
            long_ma=Decimal("100"),
            risk_on_weight=Decimal("0.70"),
            risk_off_weight=Decimal("0.35"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.80"),
        )
    )
    assert _weights_by_id(snapshot)["asset_A"] == Decimal("0.70")


def test_risk_weight_above_max_clamps_to_max() -> None:
    snapshot = _snapshot(
        _asset(
            "asset_A",
            risk_on_weight=Decimal("0.90"),
            risk_off_weight=Decimal("0.35"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.80"),
        ),
        cash_min_weight=Decimal("0.20"),
    )
    assert _weights_by_id(snapshot)["asset_A"] == Decimal("0.80")


def test_risk_weight_below_min_clamps_to_min() -> None:
    snapshot = _snapshot(
        _asset(
            "asset_A",
            current_price=Decimal("90"),
            long_ma=Decimal("100"),
            risk_on_weight=Decimal("0.70"),
            risk_off_weight=Decimal("0.05"),
            min_weight=Decimal("0.10"),
            max_weight=Decimal("0.80"),
        )
    )
    assert _weights_by_id(snapshot)["asset_A"] == Decimal("0.10")


def test_rejects_when_final_cash_falls_below_cash_floor() -> None:
    snapshot = _snapshot(
        _fixture_asset("asset_A"),
        _fixture_asset("asset_B"),
        _fixture_asset("asset_C"),
        cash_min_weight=Decimal("0.06"),
    )
    with pytest.raises(ValueError, match="cash_weight must be >= cash_min_weight"):
        allocate_rules_only_v1(snapshot)


def test_output_weights_sum_to_one_within_tolerance() -> None:
    snapshot = _snapshot(
        _fixture_asset("asset_A"),
        _fixture_asset("asset_B"),
        _fixture_asset("asset_C"),
    )
    target = allocate_rules_only_v1(snapshot)
    total = sum(weight.weight for weight in target.weights)
    assert abs(total - Decimal("1")) <= DECIMAL_WEIGHT_TOLERANCE


def test_output_includes_every_input_asset_plus_cash() -> None:
    snapshot = _snapshot(
        _fixture_asset("asset_A"),
        _fixture_asset("asset_B"),
        _fixture_asset("asset_C"),
    )
    target = allocate_rules_only_v1(snapshot)
    assert tuple(weight.asset_id for weight in target.weights) == (
        "asset_A",
        "asset_B",
        "asset_C",
        "cash",
    )


def test_output_version_and_decision_time_match_contract() -> None:
    snapshot = _snapshot(_fixture_asset("asset_A"))
    target = allocate_rules_only_v1(snapshot)
    assert target.allocator_version == RULES_ALLOCATOR_V1
    assert target.decision_time == snapshot.decision_time


def test_rules_allocator_has_no_forbidden_imports_or_calls() -> None:
    source_paths = [
        Path("src/backtest_engine/rules_allocator.py"),
        Path("src/backtest_engine/step_contract.py"),
    ]
    forbidden_import_roots = {
        "backtest_data",
        "scout",
        "allocator",
        "risk",
        "broker",
        "orders",
        "emergency",
        "composition",
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
        "random",
    }
    forbidden_call_fragments = {
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "random",
        "numpy.random",
    }

    for path in source_paths:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
                assert roots.isdisjoint(forbidden_import_roots)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".")[0] not in forbidden_import_roots

        for fragment in forbidden_call_fragments:
            assert fragment not in text
