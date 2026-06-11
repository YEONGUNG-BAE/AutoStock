"""F1a persistent trigger fire journal 테스트.

- tmp_path / :memory: 만 사용한다(runtime artifact 0).
- broker/ledger 를 import 하거나 호출하지 않는다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis.models import AnalysisAction
from domain.enums import Market
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_journal import (
    IdentityCollisionError,
    IllegalTransitionError,
    JournalResultStatus,
    JournalState,
    NonMonotonicTimestampError,
    OrderIdConflictError,
    RecordNotFoundError,
    ReserveOutcome,
    TriggerJournalError,
)

_NOW = datetime(2026, 6, 11, 9, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _FakeSignal:
    idempotency_key: str
    trigger_id: str
    decision_id: object
    plan_id: str
    market: object
    symbol: str
    action: object
    triggered_at: datetime


def _signal(**overrides: object) -> _FakeSignal:
    base: dict[str, object] = {
        "idempotency_key": "idem-1",
        "trigger_id": "trg-1",
        "decision_id": "dec-1",
        "plan_id": "plan-1",
        "market": Market.KR,
        "symbol": "005930",
        "action": AnalysisAction.BUY,
        "triggered_at": _NOW,
    }
    base.update(overrides)
    return _FakeSignal(**base)  # type: ignore[arg-type]


def _journal(tmp_path: Path) -> SqliteTriggerJournal:
    return SqliteTriggerJournal(tmp_path / "trigger_journal.sqlite3")


def _later(seconds: int) -> datetime:
    return _NOW + timedelta(seconds=seconds)


# --- reserve ---


def test_reserve_new_returns_reserved_new(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    result = journal.reserve(_signal(), _NOW)
    assert result.outcome is ReserveOutcome.RESERVED_NEW
    assert result.record.state is JournalState.RESERVED
    assert result.record.idempotency_key == "idem-1"
    assert result.record.order_id is None


def test_duplicate_reserve_creates_no_new_row(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    second = journal.reserve(_signal(), _later(1))
    assert second.outcome is ReserveOutcome.EXISTING_PENDING
    count = journal._conn.execute("SELECT COUNT(*) FROM trigger_fire_journal").fetchone()[0]
    assert count == 1


def test_sequential_two_connection_reserve_yields_one_new(tmp_path: Path) -> None:
    # 서로 다른 두 connection의 *순차* duplicate reserve (race 아님).
    path = tmp_path / "trigger_journal.sqlite3"
    journal_a = SqliteTriggerJournal(path)
    journal_b = SqliteTriggerJournal(path)
    outcome_a = journal_a.reserve(_signal(), _NOW).outcome
    outcome_b = journal_b.reserve(_signal(), _NOW).outcome
    outcomes = [outcome_a, outcome_b]
    assert outcomes.count(ReserveOutcome.RESERVED_NEW) == 1
    assert ReserveOutcome.EXISTING_PENDING in outcomes
    count = journal_a._conn.execute("SELECT COUNT(*) FROM trigger_fire_journal").fetchone()[0]
    assert count == 1


def test_threaded_concurrent_reserve_yields_exactly_one_new(tmp_path: Path) -> None:
    # threading.Barrier로 두 thread가 같은 key를 *실제 동시* reserve. 정확히 1 NEW + 1 PENDING,
    # "database is locked" 미노출(busy_timeout으로 경합 흡수).
    path = tmp_path / "trigger_journal.sqlite3"
    SqliteTriggerJournal(path).close()  # 스키마 먼저 생성(테이블 경합 제거).
    barrier = threading.Barrier(2)
    results: dict[str, ReserveOutcome] = {}
    errors: list[Exception] = []

    def worker(name: str) -> None:
        try:
            journal = SqliteTriggerJournal(path)
            barrier.wait()
            results[name] = journal.reserve(_signal(), _NOW).outcome
            journal.close()
        except Exception as exc:  # pragma: no cover - 실패 시 메인 스레드에서 surface
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    outcomes = list(results.values())
    assert outcomes.count(ReserveOutcome.RESERVED_NEW) == 1
    assert outcomes.count(ReserveOutcome.EXISTING_PENDING) == 1
    final = SqliteTriggerJournal(path)
    count = final._conn.execute("SELECT COUNT(*) FROM trigger_fire_journal").fetchone()[0]
    assert count == 1


def test_same_key_mismatched_identity_raises_collision(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    with pytest.raises(IdentityCollisionError):
        journal.reserve(_signal(symbol="000660"), _later(1))


def test_reserve_existing_terminal_signals_skip(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    journal.mark_committed("idem-1", "FILLED", _later(2))
    again = journal.reserve(_signal(), _later(3))
    assert again.outcome is ReserveOutcome.EXISTING_TERMINAL
    assert again.record.state is JournalState.COMMITTED


# --- 정상 전이 ---


def test_full_happy_path_reserve_dispatch_commit(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    dispatched = journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    assert dispatched.state is JournalState.DISPATCHING
    assert dispatched.order_id == "order-dec-1"
    assert dispatched.dispatching_at == _later(1)
    committed = journal.mark_committed("idem-1", "FILLED", _later(2))
    assert committed.state is JournalState.COMMITTED
    assert committed.result_status == "FILLED"
    assert committed.finalized_at == _later(2)


def test_reserved_can_abort(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    aborted = journal.mark_aborted("idem-1", "risk_reject", _later(1))
    assert aborted.state is JournalState.ABORTED
    assert aborted.reason_code == "risk_reject"


def test_dispatching_can_become_uncertain(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    uncertain = journal.mark_uncertain("idem-1", "restart_unknown", _later(2))
    assert uncertain.state is JournalState.UNCERTAIN
    assert uncertain.reason_code == "restart_unknown"


# --- 불법 전이 ---


@pytest.mark.parametrize(
    "operation",
    [
        lambda j: j.mark_committed("idem-1", "FILLED", _later(1)),  # RESERVED -> COMMITTED
        lambda j: j.mark_uncertain("idem-1", "x", _later(1)),  # RESERVED -> UNCERTAIN
    ],
)
def test_illegal_transition_from_reserved(tmp_path: Path, operation) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    with pytest.raises(IllegalTransitionError):
        operation(journal)


@pytest.mark.parametrize(
    "operation",
    [
        lambda j: j.mark_dispatching("idem-1", "order-x", _later(3)),  # DISPATCHING -> DISPATCHING
        lambda j: j.mark_aborted("idem-1", "x", _later(3)),  # DISPATCHING -> ABORTED
    ],
)
def test_illegal_transition_from_dispatching(tmp_path: Path, operation) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    with pytest.raises(IllegalTransitionError):
        operation(journal)


@pytest.mark.parametrize(
    "operation",
    [
        lambda j: j.mark_dispatching("idem-1", "order-x", _later(5)),
        lambda j: j.mark_committed("idem-1", "FILLED", _later(5)),
        lambda j: j.mark_aborted("idem-1", "x", _later(5)),
        lambda j: j.mark_uncertain("idem-1", "x", _later(5)),
    ],
)
def test_no_transition_out_of_terminal_committed(tmp_path: Path, operation) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    journal.mark_committed("idem-1", "FILLED", _later(2))
    with pytest.raises(IllegalTransitionError):
        operation(journal)


def test_transition_on_missing_key_raises_not_found(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(RecordNotFoundError):
        journal.mark_dispatching("nope", "order-x", _NOW)


# --- order_id UNIQUE ---


def test_duplicate_order_id_rejected(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.reserve(_signal(idempotency_key="idem-2", decision_id="dec-2"), _NOW)
    journal.mark_dispatching("idem-1", "order-shared", _later(1))
    with pytest.raises(OrderIdConflictError):
        journal.mark_dispatching("idem-2", "order-shared", _later(2))


def test_duplicate_order_id_leaves_original_row_unchanged(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.reserve(_signal(idempotency_key="idem-2", decision_id="dec-2"), _NOW)
    journal.mark_dispatching("idem-1", "order-shared", _later(1))
    with pytest.raises(OrderIdConflictError):
        journal.mark_dispatching("idem-2", "order-shared", _later(2))
    # 충돌난 행은 RESERVED 그대로(rollback), order_id 미점유.
    still_reserved = journal.get("idem-2")
    assert still_reserved is not None
    assert still_reserved.state is JournalState.RESERVED
    assert still_reserved.order_id is None


# --- 재시작 복원 ---


def test_reserved_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "trigger_journal.sqlite3"
    first = SqliteTriggerJournal(path)
    first.reserve(_signal(), _NOW)
    first.close()

    reopened = SqliteTriggerJournal(path)
    restored = reopened.get("idem-1")
    assert restored is not None
    assert restored.state is JournalState.RESERVED
    assert [r.idempotency_key for r in reopened.list_nonterminal()] == ["idem-1"]


def test_dispatching_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "trigger_journal.sqlite3"
    first = SqliteTriggerJournal(path)
    first.reserve(_signal(), _NOW)
    first.mark_dispatching("idem-1", "order-dec-1", _later(1))
    first.close()

    reopened = SqliteTriggerJournal(path)
    restored = reopened.get("idem-1")
    assert restored is not None
    assert restored.state is JournalState.DISPATCHING
    assert restored.order_id == "order-dec-1"
    assert [r.idempotency_key for r in reopened.list_nonterminal()] == ["idem-1"]


@pytest.mark.parametrize(
    "finalize",
    [
        lambda j: (
            j.mark_dispatching("idem-1", "order-dec-1", _later(1)),
            j.mark_committed("idem-1", "FILLED", _later(2)),
        ),
        lambda j: (j.mark_aborted("idem-1", "risk_reject", _later(1)),),
        lambda j: (
            j.mark_dispatching("idem-1", "order-dec-1", _later(1)),
            j.mark_uncertain("idem-1", "restart_unknown", _later(2)),
        ),
    ],
)
def test_terminal_records_excluded_from_nonterminal(tmp_path: Path, finalize) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    finalize(journal)
    assert journal.list_nonterminal() == ()


# --- 직렬화 / 검증 ---


def test_naive_datetime_now_rejected_and_leaves_no_row(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    naive = datetime(2026, 6, 11, 9, 0, 0)
    with pytest.raises(ValueError):
        journal.reserve(_signal(), naive)
    assert journal.get("idem-1") is None


def test_naive_triggered_at_rejected(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.reserve(_signal(triggered_at=datetime(2026, 6, 11, 9, 0, 0)), _NOW)


def test_bool_identity_field_rejected(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(TriggerJournalError):
        journal.reserve(_signal(symbol=True), _NOW)


def test_unknown_enum_value_rejected(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(TriggerJournalError):
        journal.reserve(_signal(action="teleport"), _NOW)


def test_string_enum_values_are_accepted(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    record = journal.reserve(_signal(market="US", action="sell"), _NOW).record
    assert record.market == "US"
    assert record.action == "sell"


def test_round_trip_datetimes_are_timezone_aware(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    record = journal.mark_committed("idem-1", "FILLED", _later(2))
    for value in (record.triggered_at, record.reserved_at, record.dispatching_at, record.finalized_at):
        assert value is not None
        assert value.tzinfo is not None


# --- transaction rollback ---


def test_transaction_rollback_leaves_no_partial_row(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(RuntimeError):
        with journal.transaction():
            journal._conn.execute(
                """
                INSERT INTO trigger_fire_journal (
                    idempotency_key, trigger_id, decision_id, plan_id, market, symbol,
                    action, state, triggered_at, reserved_at, updated_at
                ) VALUES ('idem-x','t','d','p','KR','s','buy','reserved',?,?,?)
                """,
                (_NOW.isoformat(), _NOW.isoformat(), _NOW.isoformat()),
            )
            raise RuntimeError("boom")
    assert journal.get("idem-x") is None


# --- terminal 전이 회귀(ABORTED / UNCERTAIN) ---


@pytest.mark.parametrize(
    "operation",
    [
        lambda j: j.mark_dispatching("idem-1", "order-x", _later(5)),
        lambda j: j.mark_committed("idem-1", "FILLED", _later(5)),
        lambda j: j.mark_aborted("idem-1", "x", _later(5)),
        lambda j: j.mark_uncertain("idem-1", "x", _later(5)),
    ],
)
def test_no_transition_out_of_terminal_aborted(tmp_path: Path, operation) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_aborted("idem-1", "risk_reject", _later(1))
    with pytest.raises(IllegalTransitionError):
        operation(journal)


@pytest.mark.parametrize(
    "operation",
    [
        lambda j: j.mark_dispatching("idem-1", "order-x", _later(5)),
        lambda j: j.mark_committed("idem-1", "FILLED", _later(5)),
        lambda j: j.mark_aborted("idem-1", "x", _later(5)),
        lambda j: j.mark_uncertain("idem-1", "x", _later(5)),
    ],
)
def test_no_transition_out_of_terminal_uncertain(tmp_path: Path, operation) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    journal.mark_uncertain("idem-1", "restart_unknown", _later(2))
    with pytest.raises(IllegalTransitionError):
        operation(journal)


def test_repeated_commit_rejected(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    journal.mark_committed("idem-1", "FILLED", _later(2))
    with pytest.raises(IllegalTransitionError):
        journal.mark_committed("idem-1", "FILLED", _later(3))


# --- result_status enum 검증 ---


def test_commit_rejects_unknown_result_status(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    with pytest.raises(TriggerJournalError):
        journal.mark_committed("idem-1", "teleport", _later(2))


def test_commit_rejects_bool_result_status(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    with pytest.raises(TriggerJournalError):
        journal.mark_committed("idem-1", True, _later(2))  # type: ignore[arg-type]


def test_commit_accepts_journal_result_status_enum(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(1))
    record = journal.mark_committed("idem-1", JournalResultStatus.PARTIALLY_FILLED, _later(2))
    assert record.result_status == "PARTIALLY_FILLED"


# --- timestamp 단조성 ---


def test_reserve_rejects_triggered_after_reserved(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(NonMonotonicTimestampError):
        journal.reserve(_signal(triggered_at=_later(10)), _NOW)
    assert journal.get("idem-1") is None


def test_transition_rejects_backward_time(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    journal.mark_dispatching("idem-1", "order-dec-1", _later(5))
    with pytest.raises(NonMonotonicTimestampError):
        journal.mark_committed("idem-1", "FILLED", _later(1))
    # 역행 시도는 거부되고 상태/시각은 보존된다(rollback).
    record = journal.get("idem-1")
    assert record is not None
    assert record.state is JournalState.DISPATCHING


def test_transition_allows_equal_time(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_signal(), _NOW)
    record = journal.mark_dispatching("idem-1", "order-dec-1", _NOW)
    assert record.state is JournalState.DISPATCHING


# --- mixed-offset 정렬 / UTC canonical 저장 ---


def test_list_nonterminal_orders_by_absolute_time_across_offsets(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    kst = timezone(timedelta(hours=9))
    # A: 2026-06-11T09:00+09:00 == 00:00Z (절대적으로 더 이름)
    a_at = datetime(2026, 6, 11, 9, 0, 0, tzinfo=kst)
    # B: 2026-06-11T08:00+00:00 == 08:00Z (절대적으로 더 늦음)
    b_at = datetime(2026, 6, 11, 8, 0, 0, tzinfo=timezone.utc)
    # raw isoformat 문자열 정렬이면 "08:00..+00:00" < "09:00..+09:00" 라 B가 먼저 와 틀린다.
    journal.reserve(_signal(idempotency_key="A", decision_id="dec-a", triggered_at=a_at), a_at)
    journal.reserve(_signal(idempotency_key="B", decision_id="dec-b", triggered_at=b_at), b_at)
    keys = [record.idempotency_key for record in journal.list_nonterminal()]
    assert keys == ["A", "B"]


def test_stored_datetimes_are_normalized_to_utc(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    kst = timezone(timedelta(hours=9))
    at = datetime(2026, 6, 11, 9, 0, 0, tzinfo=kst)
    record = journal.reserve(_signal(triggered_at=at), at).record
    assert record.reserved_at.utcoffset() == timedelta(0)
    assert record.triggered_at.utcoffset() == timedelta(0)
    assert record.reserved_at == at  # 동일 절대시각
