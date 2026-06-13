"""RTM-7c.1 — offline calendar-gated decision refresh scheduler tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from analysis.models import ANALYSIS_DECISION_SCHEMA
from domain import DateId, DecisionId, Percent
from domain.decision import DecisionSnapshot
from domain.enums import Market
from domain.validation import ValidationResult
from market_data.market_session import (
    ExplicitMarketScheduleProvider,
    SessionWindow,
    build_explicit_schedule,
)
from orchestration.active_decision_store import (
    ActiveDecisionStore,
    DecisionPublicationCandidate,
    SlotState,
)
from orchestration.decision_refresh_scheduler import (
    DecisionRefreshScheduler,
    RefreshSlotOutcome,
    SchedulerState,
    SlotConfig,
)

_KST = ZoneInfo("Asia/Seoul")
_DAY = date(2026, 6, 15)  # Monday
_WINDOW = SessionWindow(
    pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
)
_SLOTS = (
    SlotConfig(slot_id="s1", at=time(9, 30)),
    SlotConfig(slot_id="s2", at=time(11, 0)),
    SlotConfig(slot_id="s3", at=time(13, 0)),
    SlotConfig(slot_id="s4", at=time(14, 50)),
)
DAY_DELTA = timedelta(days=1)


def _at(t: time, *, second: int = 1) -> datetime:
    return datetime.combine(_DAY, t.replace(second=second), tzinfo=_KST)


def _reason(date_id: str = "260615-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _hold_decision(decision_id: str, created_at: datetime) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        universe="KR_LARGE",
        symbol="005930",
        market="KR",
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260615-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260615-3"),)),
        fund_manager=FundManagerDecision(
            action=AnalysisAction.HOLD,
            target_weight_percent=Percent("5"),
            rationale="근거",
            reasons=(_reason("260615-4"),),
        ),
        reasons=(_reason("260615-5"),),
    )


class _HoldRunner:
    """slot마다 scheduled_at을 created_at으로 갖는 새 HOLD 후보를 만든다(매번 PUBLISHED)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def refresh(
        self, *, market: Market, session_date: date, slot_id: str, scheduled_at: datetime
    ) -> DecisionPublicationCandidate:
        self.calls.append(slot_id)
        decision = _hold_decision(f"d-{session_date.isoformat()}-{slot_id}", scheduled_at)
        snapshot = DecisionSnapshot.create(
            decision_id=decision.decision_id,
            created_at=decision.created_at,
            schema_name=ANALYSIS_DECISION_SCHEMA,
            raw_payload=decision.model_dump(mode="json"),
            validation_result=ValidationResult(
                passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA
            ),
        )
        return DecisionPublicationCandidate(
            snapshot=snapshot,
            plan=None,
            valid_from=scheduled_at,
            expires_at=scheduled_at + DAY_DELTA,
        )


class _BoomRunner:
    async def refresh(self, **_: object) -> DecisionPublicationCandidate:
        raise RuntimeError("runner boom")


class _WrongMarketRunner:
    """scheduler가 KR을 요청해도 US 후보를 반환하는 buggy runner(market binding 검증용)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def refresh(
        self, *, market: Market, session_date: date, slot_id: str, scheduled_at: datetime
    ) -> DecisionPublicationCandidate:
        self.calls.append(slot_id)
        decision = _hold_decision(f"us-{slot_id}", scheduled_at).model_copy(
            update={"market": "US"}
        )
        snapshot = DecisionSnapshot.create(
            decision_id=decision.decision_id,
            created_at=decision.created_at,
            schema_name=ANALYSIS_DECISION_SCHEMA,
            raw_payload=decision.model_dump(mode="json"),
            validation_result=ValidationResult(
                passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA
            ),
        )
        return DecisionPublicationCandidate(
            snapshot=snapshot,
            plan=None,
            valid_from=scheduled_at,
            expires_at=scheduled_at + DAY_DELTA,
        )


class _NeverRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, **_: object) -> DecisionPublicationCandidate:
        self.calls += 1
        raise AssertionError("runner must not be called")


class _BoomCalendar:
    def session_at(self, market: Market, instant: datetime):  # noqa: ANN201
        raise RuntimeError("calendar provider boom")

    def is_trading_day(self, market: Market, day: date) -> bool:
        return False


class _ClosedCalendar:
    def session_at(self, market: Market, instant: datetime):  # noqa: ANN201
        from market_data.market_session import MarketSession, MarketSessionState

        return MarketSession(market=market, state=MarketSessionState.CLOSED, as_of=instant)

    def is_trading_day(self, market: Market, day: date) -> bool:
        return False


class _PostCloseTradingCalendar:
    """거래일이며 세션이 종료(POST_CLOSE)된 상태를 모사한다(early/normal close 이후)."""

    def session_at(self, market: Market, instant: datetime):  # noqa: ANN201
        from market_data.market_session import MarketSession, MarketSessionState

        return MarketSession(
            market=market, state=MarketSessionState.POST_CLOSE, as_of=instant
        )

    def is_trading_day(self, market: Market, day: date) -> bool:
        return True


class _SequencedClock:
    def __init__(self, times: Sequence[datetime]) -> None:
        self._times = list(times)
        self._i = 0

    def __call__(self) -> datetime:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


async def _fake_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _open_calendar() -> ExplicitMarketScheduleProvider:
    return build_explicit_schedule(timezone=_KST, trading_days=[_DAY], window=_WINDOW)


def _scheduler(
    *,
    calendar: object,
    runner: object,
    store: ActiveDecisionStore,
    clock: object,
    max_ticks: int,
    slot_grace_seconds: float = 300.0,
) -> DecisionRefreshScheduler:
    return DecisionRefreshScheduler(
        market=Market.KR,
        calendar=calendar,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        store=store,
        slots=_SLOTS,
        timezone=_KST,
        clock=clock,  # type: ignore[arg-type]
        sleep=_fake_sleep,
        poll_interval_seconds=0.01,
        slot_grace_seconds=slot_grace_seconds,
        max_ticks=max_ticks,
    )


def test_four_slots_run_exactly_once(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _HoldRunner()
    # 각 tick은 now(due 판정) + completion(runner 완료 후 재읽기) 2회 clock을 읽는다.
    clock = _SequencedClock(
        [
            _at(time(9, 30)), _at(time(9, 30)),
            _at(time(11, 0)), _at(time(11, 0)),
            _at(time(13, 0)), _at(time(13, 0)),
            _at(time(14, 50)), _at(time(14, 50)),
        ]
    )
    sup = _scheduler(
        calendar=_open_calendar(), runner=runner, store=store, clock=clock, max_ticks=4
    )
    summary = asyncio.run(sup.run())
    assert summary.slots_run == 4
    assert summary.slots_missed == 0
    assert runner.calls == ["s1", "s2", "s3", "s4"]
    assert len(store.list_history(Market.KR, "005930")) == 4


def test_missed_slots_no_catchup_burst(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _HoldRunner()
    # start late: well after all four slots, but the most-recent is within grace.
    clock = _SequencedClock([_at(time(14, 50), second=30)])
    sup = _scheduler(
        calendar=_open_calendar(),
        runner=runner,
        store=store,
        clock=clock,
        max_ticks=1,
        slot_grace_seconds=600.0,
    )
    summary = asyncio.run(sup.run())
    assert summary.slots_run == 1  # only the most-recent slot, no burst
    assert summary.slots_missed == 3
    assert runner.calls == ["s4"]


def test_calendar_missing_refreshes_zero(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _HoldRunner()
    # explicit schedule WITHOUT _DAY → weekday missing → UNKNOWN/CALENDAR_MISSING.
    empty_calendar = ExplicitMarketScheduleProvider(timezone=_KST, schedule={})
    clock = _SequencedClock([_at(time(11, 0)), _at(time(13, 0))])
    sup = _scheduler(
        calendar=empty_calendar, runner=runner, store=store, clock=clock, max_ticks=2
    )
    summary = asyncio.run(sup.run())
    assert summary.slots_run == 0
    assert summary.slots_missed == 0
    assert runner.calls == []
    assert store.read_active(Market.KR, "005930") is None


def test_provider_failure_is_terminal(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _NeverRunner()
    clock = _SequencedClock([_at(time(11, 0))])
    sup = _scheduler(
        calendar=_BoomCalendar(), runner=runner, store=store, clock=clock, max_ticks=5
    )
    summary = asyncio.run(sup.run())
    assert summary.final_state is SchedulerState.FAILED_CLOSED
    assert summary.ticks == 1
    assert runner.calls == 0


def test_runner_failure_keeps_previous_bundle_no_retry(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    # first slot publishes via a good runner, second slot's runner raises.
    good = _HoldRunner()
    clock = _SequencedClock([_at(time(9, 30))])
    asyncio.run(
        _scheduler(
            calendar=_open_calendar(), runner=good, store=store, clock=clock, max_ticks=1
        ).run()
    )
    before = store.read_active(Market.KR, "005930")
    assert before is not None

    boom = _BoomRunner()
    clock2 = _SequencedClock([_at(time(11, 0)), _at(time(11, 0), second=5)])
    sup = _scheduler(
        calendar=_open_calendar(), runner=boom, store=store, clock=clock2, max_ticks=2
    )
    summary = asyncio.run(sup.run())
    # runner failed once; slot consumed (no infinite retry); previous bundle preserved.
    assert summary.runner_failures == 1
    after = store.read_active(Market.KR, "005930")
    assert after is not None and after.decision_id == before.decision_id


def test_cancellation_leaks_no_tasks(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _NeverRunner()
    clock = _SequencedClock([_at(time(20, 0))])  # after close → CLOSED, loops idle

    sup = DecisionRefreshScheduler(
        market=Market.KR,
        calendar=_ClosedCalendar(),
        runner=runner,  # type: ignore[arg-type]
        store=store,
        slots=_SLOTS,
        timezone=_KST,
        clock=clock,
        sleep=_fake_sleep,
        poll_interval_seconds=0.01,
        max_ticks=None,
    )

    async def scenario() -> None:
        task = asyncio.create_task(sup.run())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leaked = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leaked == []

    asyncio.run(scenario())
    assert runner.calls == 0


def test_durable_slot_not_rerun_after_restart(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    # 1차 인스턴스가 s1을 실행/게시한다.
    run1 = _HoldRunner()
    asyncio.run(
        _scheduler(
            calendar=_open_calendar(),
            runner=run1,
            store=store,
            clock=_SequencedClock([_at(time(9, 30))]),
            max_ticks=1,
        ).run()
    )
    assert run1.calls == ["s1"]
    assert store.slot_states(Market.KR, _DAY)["s1"] is SlotState.PUBLISHED

    # 2차 인스턴스(재시작): in-memory 상태 없음. durable journal로 s1 재실행 금지.
    run2 = _NeverRunner()
    summary = asyncio.run(
        _scheduler(
            calendar=_open_calendar(),
            runner=run2,
            store=store,
            clock=_SequencedClock([_at(time(9, 30), second=30)]),
            max_ticks=1,
            slot_grace_seconds=600.0,
        ).run()
    )
    assert run2.calls == 0
    assert summary.slots_run == 0


def test_dangling_reserved_reconciled_not_rerun(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    # 직전 프로세스가 s1을 예약만 하고 죽음(RESERVED 잔존)을 모사한다. lease는 곧 만료된다.
    store.reserve_slot(
        market=Market.KR,
        session_date=_DAY,
        slot_id="s1",
        scheduled_at=_at(time(9, 30), second=0),
        owner_id="dead-process",
        now=_at(time(9, 30)),
        lease_seconds=10.0,
    )
    runner = _NeverRunner()
    summary = asyncio.run(
        _scheduler(
            calendar=_open_calendar(),
            runner=runner,
            store=store,
            clock=_SequencedClock([_at(time(9, 30), second=30)]),
            max_ticks=1,
            slot_grace_seconds=600.0,
        ).run()
    )
    # 잔존 RESERVED는 UNCERTAIN으로 reconcile되고 재실행되지 않는다(fail-closed).
    assert runner.calls == 0
    assert summary.slots_reconciled == 1
    assert store.slot_states(Market.KR, _DAY)["s1"] is SlotState.UNCERTAIN


def test_evidence_sink_failure_is_terminal_failed_closed(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _HoldRunner()

    def _boom_sink(_evidence: object) -> None:
        raise RuntimeError("super-secret-payload-or-credential")

    sup = DecisionRefreshScheduler(
        market=Market.KR,
        calendar=_open_calendar(),
        runner=runner,
        store=store,
        slots=_SLOTS,
        timezone=_KST,
        clock=_SequencedClock([_at(time(14, 50), second=30)]),
        sleep=_fake_sleep,
        poll_interval_seconds=0.01,
        slot_grace_seconds=600.0,
        max_ticks=3,
        on_evidence=_boom_sink,
    )
    summary = asyncio.run(sup.run())
    assert summary.final_state is SchedulerState.FAILED_CLOSED
    # 원시 예외 문자열이 evidence에 누출되지 않는다.
    for ev in summary.evidence:
        assert ev.reason is None or "secret" not in ev.reason


def test_early_close_marks_session_closed(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _NeverRunner()
    # 거래일이며 세션 종료(POST_CLOSE), 모든 slot 시각이 지났음.
    sup = _scheduler(
        calendar=_PostCloseTradingCalendar(),
        runner=runner,
        store=store,
        clock=_SequencedClock([_at(time(15, 45))]),
        max_ticks=1,
    )
    summary = asyncio.run(sup.run())
    assert runner.calls == 0
    assert summary.slots_run == 0
    assert summary.slots_missed_session_closed == 4
    states = store.slot_states(Market.KR, _DAY)
    assert all(s is SlotState.MISSED_SESSION_CLOSED for s in states.values())
    assert len(states) == 4


def test_early_close_marks_future_slots_too(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _NeverRunner()
    # 12:00 조기폐장(POST_CLOSE): s1@9:30, s2@11:00은 지났고 s3@13:00, s4@14:50은 미도래.
    sup = _scheduler(
        calendar=_PostCloseTradingCalendar(),
        runner=runner,
        store=store,
        clock=_SequencedClock([_at(time(12, 0))]),
        max_ticks=1,
    )
    summary = asyncio.run(sup.run())
    assert runner.calls == 0
    # 세션이 끝났으므로 미도래 slot도 당일 실행되지 않음 → 전부 MISSED_SESSION_CLOSED.
    assert summary.slots_missed_session_closed == 4
    states = store.slot_states(Market.KR, _DAY)
    assert states["s3"] is SlotState.MISSED_SESSION_CLOSED
    assert states["s4"] is SlotState.MISSED_SESSION_CLOSED


def test_scheduler_candidate_market_mismatch_fails_closed(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _WrongMarketRunner()
    # tick now + completion 재읽기 2회.
    clock = _SequencedClock([_at(time(9, 30)), _at(time(9, 30))])
    sup = _scheduler(
        calendar=_open_calendar(), runner=runner, store=store, clock=clock, max_ticks=1
    )
    summary = asyncio.run(sup.run())
    # runner는 호출되었지만 US 후보는 KR 스케줄러에서 게시 거부 → PUBLISH_FAILED, 활성 없음.
    assert runner.calls == ["s1"]
    assert summary.slots_run == 0
    assert summary.publish_failures == 1
    assert store.read_active(Market.KR, "005930") is None
    assert store.read_active("US", "005930") is None
    assert store.slot_states(Market.KR, _DAY)["s1"] is SlotState.FAILED


class _LeaseStealingRunner:
    """runner 실행 도중 다른 인스턴스가 lease 만료를 보고 reconcile했다고 모사한다.

    refresh() 안에서 같은 db에 두 번째 store를 열어 예약을 UNCERTAIN으로 reconcile한다 →
    완료 후 게시 시점에 owner는 더 이상 예약을 보유하지 않는다(REJECTED_RESERVATION_LOST)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self.calls: list[str] = []

    async def refresh(
        self, *, market: Market, session_date: date, slot_id: str, scheduled_at: datetime
    ) -> DecisionPublicationCandidate:
        self.calls.append(slot_id)
        other = ActiveDecisionStore(self._db_path)
        try:
            # lease가 만료된 것으로 보이게 충분히 미래 시각으로 reconcile.
            other.reconcile_expired_reservation(
                market=market,
                session_date=session_date,
                slot_id=slot_id,
                now=scheduled_at + timedelta(hours=2),
                outcome="stolen",
            )
        finally:
            other.close()
        decision = _hold_decision(f"d-{slot_id}", scheduled_at)
        snapshot = DecisionSnapshot.create(
            decision_id=decision.decision_id,
            created_at=decision.created_at,
            schema_name=ANALYSIS_DECISION_SCHEMA,
            raw_payload=decision.model_dump(mode="json"),
            validation_result=ValidationResult(
                passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA
            ),
        )
        return DecisionPublicationCandidate(
            snapshot=snapshot,
            plan=None,
            valid_from=scheduled_at,
            expires_at=scheduled_at + DAY_DELTA,
        )


def test_lease_lost_during_runner_blocks_publish_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite3"
    store = ActiveDecisionStore(path)
    runner = _LeaseStealingRunner(path)
    # tick now(9:30) + completion 재읽기(9:30). reconcile는 runner 내부에서 미래 시각으로 수행.
    clock = _SequencedClock([_at(time(9, 30)), _at(time(9, 30))])
    sup = _scheduler(
        calendar=_open_calendar(), runner=runner, store=store, clock=clock, max_ticks=1
    )
    summary = asyncio.run(sup.run())
    # runner는 호출됐지만 예약을 잃어 게시되지 않는다(active pointer 불변) → terminal fail-closed.
    assert runner.calls == ["s1"]
    assert summary.slots_run == 0
    assert summary.slots_published == 0
    assert summary.publish_failures == 1
    assert summary.final_state is SchedulerState.FAILED_CLOSED
    assert store.read_active(Market.KR, "005930") is None
    # slot은 reconcile된 UNCERTAIN 그대로(뒤늦은 owner가 덮지 못함), 중복 게시 없음.
    assert store.slot_states(Market.KR, _DAY)["s1"] is SlotState.UNCERTAIN
    assert store.list_history(Market.KR, "005930") == ()
    store.close()


class _CloseAfterOpenCalendar:
    """tick 시점엔 OPEN, runner 완료 후 재검증 시점엔 CLOSED를 반환한다(장 마감 후 완료 모사)."""

    def __init__(self, close_after: datetime) -> None:
        self._close_after = close_after

    def session_at(self, market: Market, instant: datetime):  # noqa: ANN201
        from market_data.market_session import MarketSession, MarketSessionState

        state = (
            MarketSessionState.OPEN
            if instant < self._close_after
            else MarketSessionState.CLOSED
        )
        return MarketSession(market=market, state=state, as_of=instant)

    def is_trading_day(self, market: Market, day: date) -> bool:
        return True


def test_completion_after_close_does_not_publish(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "a.sqlite3")
    runner = _HoldRunner()
    # tick now=14:51(OPEN, s4@14:50 grace 내), runner 완료 시각=15:40(>15:30 → CLOSED) → 게시 금지.
    clock = _SequencedClock([_at(time(14, 51)), _at(time(15, 40))])
    sup = _scheduler(
        calendar=_CloseAfterOpenCalendar(_at(time(15, 30))),
        runner=runner,
        store=store,
        clock=clock,
        max_ticks=1,
    )
    summary = asyncio.run(sup.run())
    # runner는 완료됐지만 완료 시점 장이 닫혀 게시하지 않는다(직전 bundle 유지: 여기선 없음).
    assert runner.calls == ["s4"]  # 14:51에 가장 최근 due slot은 s4(14:50).
    assert summary.slots_run == 0
    assert summary.slots_published == 0
    assert summary.slots_missed_session_closed == 1
    assert store.read_active(Market.KR, "005930") is None
    assert store.slot_states(Market.KR, _DAY)["s4"] is SlotState.MISSED_SESSION_CLOSED
    assert store.list_history(Market.KR, "005930") == ()
