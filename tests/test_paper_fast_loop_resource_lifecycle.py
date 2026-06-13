"""RTM-7c.4b resource-lifecycle regression tests for the offline fast-loop stack.

These prove the durable SQLite handles owned by ``PaperFastLoopStack`` are released
deterministically:

* a normally built stack closes every handle exactly once (idempotent on re-close);
* a context-body exception still closes every handle;
* a *partial* construction (any store constructor or later in-memory dependency raising)
  closes every already-opened SQLite handle in reverse order and re-raises the **original**
  exception — a cleanup-time ``close`` failure never masks it;
* the same durable files can be reopened after close (composition restart);
* the temp dir is deletable afterwards (no leaked OS handles).

The harness wraps the *real* ``SQLiteLedger`` / ``SqliteTriggerJournal`` /
``ActiveDecisionStore`` in a close-counting spy so we exercise genuine SQLite handles
while still observing the lifecycle, and injects failures by monkeypatching the relevant
constructor in the composition module. All spies append to a single shared ordered
close-event log, so the tests prove the exact teardown *order* (construction is
ledger → journal → active; teardown reverses it to active → journal → ledger on both the
normal ``close()`` path and the partial-construction cleanup path) — not merely that each
handle was closed once.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition import paper_fast_loop as _pfl
from composition.paper_fast_loop import (
    PaperFastLoopStack,
    build_offline_paper_fast_loop_stack,
    build_replay_snapshot_payload,
)
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from ledger.sqlite_ledger import SQLiteLedger
from orchestration.active_decision_store import ActiveDecisionStore
from orchestration.execution_inputs_snapshot import (
    ValidatedExecutionInputsProvider,
    load_execution_inputs_snapshot,
)


class _CloseSpy:
    """Delegates everything to a real store but counts ``close()`` calls and records the
    close *order* into a shared event log.

    ``role`` labels which store this wraps; on every ``close()`` the spy appends ``role`` to
    the shared ``events`` list BEFORE delegating, so a single ordered log across all three
    spies proves the teardown sequence (not merely that each handle was closed once).
    ``fail_close`` makes ``close()`` raise *after* incrementing the counter and recording the
    event, to model a teardown error that must not abort cleanup of the remaining handles."""

    def __init__(
        self, inner: Any, *, role: str = "", events: list[str] | None = None,
        fail_close: bool = False,
    ) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_fail_close", fail_close)
        object.__setattr__(self, "_role", role)
        object.__setattr__(self, "_events", events)
        object.__setattr__(self, "close_calls", 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def close(self) -> None:
        object.__setattr__(self, "close_calls", self.close_calls + 1)
        events = object.__getattribute__(self, "_events")
        if events is not None:
            events.append(object.__getattribute__(self, "_role"))
        # 실제 핸들은 항상 닫아 OS 핸들 누수를 막는다(테스트가 실패 신호만 추가로 던질 때도).
        object.__getattribute__(self, "_inner").close()
        if object.__getattribute__(self, "_fail_close"):
            raise RuntimeError("close boom")


def _provider(tmp_path: Path) -> ValidatedExecutionInputsProvider:
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(build_replay_snapshot_payload()), encoding="utf-8")
    return ValidatedExecutionInputsProvider(snapshot=load_execution_inputs_snapshot(snap))


def _install_store_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_on: str | None = None,
    fail_close: tuple[str, ...] = (),
) -> tuple[dict[str, _CloseSpy], list[str]]:
    """Monkeypatch the three store constructors in the composition module.

    ``fail_on`` (one of ``"ledger"``/``"journal"``/``"active"``) makes that constructor
    raise. Every other store is built for real and wrapped in a ``_CloseSpy`` recorded in
    the returned dict keyed by role. Returns ``(created, events)`` where ``events`` is the
    shared ordered close-event log all spies append to, so callers can assert the exact
    teardown order (construction is ledger → journal → active; teardown reverses it)."""

    created: dict[str, _CloseSpy] = {}
    events: list[str] = []

    def _factory(role: str, real_cls: Any):
        def _make(*args: Any, **kwargs: Any) -> _CloseSpy:
            if fail_on == role:
                raise RuntimeError(f"{role} constructor boom")
            spy = _CloseSpy(
                real_cls(*args, **kwargs), role=role, events=events, fail_close=role in fail_close
            )
            created[role] = spy
            return spy

        return _make

    monkeypatch.setattr(_pfl, "SQLiteLedger", _factory("ledger", SQLiteLedger))
    monkeypatch.setattr(_pfl, "SqliteTriggerJournal", _factory("journal", SqliteTriggerJournal))
    monkeypatch.setattr(_pfl, "ActiveDecisionStore", _factory("active", ActiveDecisionStore))
    return created, events


def test_normal_stack_closes_each_handle_once_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, events = _install_store_spies(monkeypatch)
    with build_offline_paper_fast_loop_stack(tmp_path, provider=_provider(tmp_path)) as stack:
        assert isinstance(stack, PaperFastLoopStack)
    assert {role: spy.close_calls for role, spy in created.items()} == {
        "ledger": 1,
        "journal": 1,
        "active": 1,
    }
    # 정상 teardown은 생성 역순(active → journal → ledger)으로 닫는다.
    assert events == ["active", "journal", "ledger"]
    # 재호출은 멱등 — 추가 close 없음(이벤트 로그도 변하지 않는다).
    stack.close()
    assert all(spy.close_calls == 1 for spy in created.values())
    assert events == ["active", "journal", "ledger"]


def test_context_body_exception_still_closes_every_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, events = _install_store_spies(monkeypatch)
    with pytest.raises(RuntimeError, match="body boom"):
        with build_offline_paper_fast_loop_stack(tmp_path, provider=_provider(tmp_path)):
            raise RuntimeError("body boom")
    assert all(spy.close_calls == 1 for spy in created.values())
    # 본문 예외 경로에서도 teardown 순서는 동일하다(active → journal → ledger).
    assert events == ["active", "journal", "ledger"]


def test_journal_constructor_failure_closes_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, events = _install_store_spies(monkeypatch, fail_on="journal")
    with pytest.raises(RuntimeError, match="journal constructor boom"):
        _pfl._build_stack(tmp_path, provider=_provider(tmp_path))
    assert set(created) == {"ledger"}
    assert created["ledger"].close_calls == 1
    # 오직 ledger만 열렸으므로 정리도 ledger 하나뿐이다.
    assert events == ["ledger"]


def test_active_store_constructor_failure_closes_journal_and_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, events = _install_store_spies(monkeypatch, fail_on="active")
    with pytest.raises(RuntimeError, match="active constructor boom"):
        _pfl._build_stack(tmp_path, provider=_provider(tmp_path))
    assert set(created) == {"ledger", "journal"}
    assert created["ledger"].close_calls == 1
    assert created["journal"].close_calls == 1
    # active는 생성 실패 → ledger/journal만 역순(journal → ledger)으로 정리된다.
    assert events == ["journal", "ledger"]


def test_later_in_memory_dependency_failure_closes_all_sqlite_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 세 SQLite 핸들이 모두 열린 뒤 in-memory 의존성 생성이 실패하면 셋 다 닫혀야 한다.
    created, events = _install_store_spies(monkeypatch)

    def _boom(*_a: Any, **_k: Any):
        raise RuntimeError("latest store boom")

    monkeypatch.setattr(_pfl, "LatestMarketStateStore", _boom)
    with pytest.raises(RuntimeError, match="latest store boom"):
        _pfl._build_stack(tmp_path, provider=_provider(tmp_path))
    assert set(created) == {"ledger", "journal", "active"}
    assert all(spy.close_calls == 1 for spy in created.values())
    # 부분-생성 정리도 생성 역순(active → journal → ledger)이다.
    assert events == ["active", "journal", "ledger"]


def test_cleanup_close_failure_preserves_original_exception_and_attempts_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # journal.close가 실패해도 ledger/active는 닫히고, 원래 construction 예외가 전파된다.
    created, events = _install_store_spies(monkeypatch, fail_close=("journal",))

    def _boom(*_a: Any, **_k: Any):
        raise RuntimeError("original construction boom")

    monkeypatch.setattr(_pfl, "LatestMarketStateStore", _boom)
    with pytest.raises(RuntimeError, match="original construction boom"):
        _pfl._build_stack(tmp_path, provider=_provider(tmp_path))
    # cleanup 순서는 active → journal(실패, 삼킴) → ledger. 셋 다 시도된다(중간 실패가 나머지를
    # 막지 않는다). 공유 이벤트 로그가 "모두 시도됨 + 정확한 순서"를 동시에 증명한다.
    assert created["active"].close_calls == 1
    assert created["journal"].close_calls == 1
    assert created["ledger"].close_calls == 1
    assert events == ["active", "journal", "ledger"]


def test_restart_reopens_same_durable_files_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    first, _first_events = _install_store_spies(monkeypatch)
    with build_offline_paper_fast_loop_stack(tmp_path, provider=provider):
        pass
    assert all(spy.close_calls == 1 for spy in first.values())
    # 같은 디렉터리(같은 DB 파일)로 재구성 — 핸들 누수 없이 다시 열려야 한다.
    second, _second_events = _install_store_spies(monkeypatch)
    with build_offline_paper_fast_loop_stack(tmp_path, provider=provider):
        pass
    assert all(spy.close_calls == 1 for spy in second.values())


def test_temp_dir_is_deletable_after_close(tmp_path: Path) -> None:
    work = tmp_path / "stack"
    work.mkdir()
    with build_offline_paper_fast_loop_stack(work, provider=_provider(tmp_path)):
        pass
    shutil.rmtree(work)
    assert not work.exists()
