from __future__ import annotations

from paper_loop.models import PaperLoopInput, PaperLoopResult
from paper_loop.runner import PaperLoopRunner


def replay_paper_loop(
    runner: PaperLoopRunner,
    loop_input: PaperLoopInput,
) -> PaperLoopResult:
    """동일 입력으로 paper loop를 재실행한다. replay determinism 테스트용."""
    return runner.run(loop_input)


def assert_same_risk_result(first: PaperLoopResult, second: PaperLoopResult) -> None:
    """동일 입력 replay 시 risk validation_result가 같아야 한다."""
    assert first.risk_result.to_canonical_dict() == second.risk_result.to_canonical_dict()


def assert_same_generated_intent(first: PaperLoopResult, second: PaperLoopResult) -> None:
    """동일 입력 replay 시 generated OrderIntent가 같아야 한다."""
    if first.generated_order_intent is None:
        assert second.generated_order_intent is None
        return
    assert second.generated_order_intent is not None
    assert (
        first.generated_order_intent.model_dump(mode="json")
        == second.generated_order_intent.model_dump(mode="json")
    )


def assert_same_decision_snapshot_hash(
    store,
    decision_id,
) -> None:
    """DecisionSnapshot payload_hash가 결정적이어야 한다."""
    first = store.get_decision_snapshot(decision_id)
    assert first is not None
    second = store.get_decision_snapshot(decision_id)
    assert second is not None
    assert first.payload_hash == second.payload_hash
