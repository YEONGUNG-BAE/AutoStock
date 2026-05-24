from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from domain import Money, Percent
from domain.enums import Currency
from risk import AssetClassWeights, OrderIntentGenerator, RiskFilter, RiskMode


def test_replay_risk_filter_result_is_deterministic(sample_risk_input_factory) -> None:
    risk_filter = RiskFilter()
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )

    first = risk_filter.evaluate(risk_input)
    second = risk_filter.evaluate(risk_input)

    assert first.to_canonical_dict() == second.to_canonical_dict()


def test_replay_order_generation_is_deterministic(sample_risk_input_factory) -> None:
    generator = OrderIntentGenerator()
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        correlation_id="replay-corr",
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )

    first = generator.generate(risk_input)
    second = generator.generate(risk_input)

    assert first.status == second.status
    assert first.correlation_id == second.correlation_id
    assert first.validation_result.to_canonical_dict() == second.validation_result.to_canonical_dict()
    if first.order_intent is not None:
        assert second.order_intent is not None
        assert first.order_intent.model_dump(mode="json") == second.order_intent.model_dump(mode="json")


def test_replay_blocked_input_no_order(sample_risk_input_factory) -> None:
    generator = OrderIntentGenerator()
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "mode": RiskMode.MDD_KILLSWITCH,
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )

    first = generator.generate(risk_input)
    second = generator.generate(risk_input)

    assert first.order_intent is None
    assert second.order_intent is None
    assert first.validation_result.to_canonical_dict() == second.validation_result.to_canonical_dict()


def test_replay_issue_ordering_deterministic(sample_risk_input_factory) -> None:
    risk_filter = RiskFilter()
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("12"),
        context_overrides={
            "mode": RiskMode.MDD_KILLSWITCH,
            "allocator_symbol_target_weight": Percent("5"),
            "current_symbol_market_value": Money.from_str("1000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("4500000", Currency.KRW),
            "current_asset_weights": AssetClassWeights(
                kr=Percent("60"),
                us=Percent("10"),
                gold=Percent("30"),
            ),
        },
    )

    first = risk_filter.evaluate(risk_input)
    second = risk_filter.evaluate(risk_input)

    assert [i.code for i in first.issues] == [i.code for i in second.issues]
