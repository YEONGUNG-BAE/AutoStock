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
    clock = _SequencedClock(
        [_at(time(9, 30)), _at(time(11, 0)), _at(time(13, 0)), _at(time(14, 50))]
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
