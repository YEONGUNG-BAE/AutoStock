from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from config.settings import ExecutionMode
from domain.enums import AccountRole, AssetClass, Currency, Market, OrderSide, OrderType
from domain.identifiers import Percent
from domain.money import Money
from domain.order import Fill, OrderIntent
from domain.portfolio import NavSnapshot
from emergency.models import EmergencyTriggerEvent
from logs.models import DailyRunStatus, DailySummary
from paper_review.models import PaperReviewInput, ReviewPeriod
from postmortem_fixtures import sample_postmortem_record
from emergency_fixtures import sample_mdd_payload, sample_stock_drop_payload

NOW = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
START_DATE = date(2026, 2, 24)
END_DATE = date(2026, 5, 24)


def sample_review_period(*, calendar_days: int | None = None) -> ReviewPeriod:
    if calendar_days is None:
        return ReviewPeriod.from_dates(start_date=START_DATE, end_date=END_DATE)
    end = START_DATE + timedelta(days=calendar_days - 1)
    return ReviewPeriod.from_dates(start_date=START_DATE, end_date=end)


def sample_nav_series(
    *,
    count: int = 5,
    start_nav: Decimal = Decimal("10000000"),
    daily_change_percent: Decimal = Decimal("0.5"),
) -> tuple[NavSnapshot, ...]:
    snapshots: list[NavSnapshot] = []
    nav = start_nav
    for index in range(count):
        as_of = datetime(2026, 2, 24, 15, 0, tzinfo=UTC) + timedelta(days=index)
        cash = nav * Decimal("0.2")
        invested = nav - cash
        snapshots.append(
            NavSnapshot(
                snapshot_id=f"nav-{index:03d}",
                as_of=as_of,
                total_nav_krw=nav,
                cash_krw=cash,
                invested_krw=invested,
            )
        )
        nav = nav * (Decimal("1") + daily_change_percent / Decimal("100"))
    return tuple(snapshots)


def sample_nav_series_with_drawdown() -> tuple[NavSnapshot, ...]:
    values = [
        Decimal("10000000"),
        Decimal("10500000"),
        Decimal("9900000"),
        Decimal("9200000"),
        Decimal("9500000"),
    ]
    snapshots: list[NavSnapshot] = []
    for index, nav in enumerate(values):
        as_of = datetime(2026, 2, 24, 15, 0, tzinfo=UTC) + timedelta(days=index)
        cash = nav * Decimal("0.25")
        invested = nav - cash
        snapshots.append(
            NavSnapshot(
                snapshot_id=f"nav-dd-{index:03d}",
                as_of=as_of,
                total_nav_krw=nav,
                cash_krw=cash,
                invested_krw=invested,
            )
        )
    return tuple(snapshots)


def sample_daily_summary(
    *,
    trading_date: date = START_DATE,
    range_violation_count: int = 0,
    allocator_fallback_count: int = 0,
    validation_failed_count: int = 0,
) -> DailySummary:
    return DailySummary(
        summary_id=f"summary-{trading_date.isoformat()}",
        trading_date=trading_date,
        created_at=NOW,
        status=DailyRunStatus.COMPLETED,
        range_violation_count=range_violation_count,
        allocator_fallback_count=allocator_fallback_count,
        validation_failed_count=validation_failed_count,
    )


def sample_order_intent(
    *,
    order_id: str = "order-001",
    side: OrderSide = OrderSide.BUY,
    execution_mode: ExecutionMode = ExecutionMode.NORMAL,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id="corr-001",
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=side,
        order_type=OrderType.MARKET,
        execution_mode=execution_mode,
        quantity=Decimal("10"),
        created_at=NOW,
    )


def sample_fill(*, order_id: str = "order-001", fill_id: str = "fill-001") -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        symbol="005930",
        market=Market.KR,
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        fill_price=Decimal("70000"),
        commission=Money(amount=Decimal("100"), currency=Currency.KRW),
        tax=Money(amount=Decimal("0"), currency=Currency.KRW),
        filled_at=NOW,
    )


def sample_emergency_event(
    *,
    event_id: str = "emergency-001",
    payload_overrides: dict[str, object] | None = None,
) -> EmergencyTriggerEvent:
    overrides = payload_overrides or {}
    return EmergencyTriggerEvent(
        event_id=event_id,
        payload=sample_stock_drop_payload(**overrides),
        created_at=NOW,
    )


def sample_mdd_emergency_event(
    *,
    event_id: str = "mdd-001",
    stage: str = "LEVEL_1",
) -> EmergencyTriggerEvent:
    return EmergencyTriggerEvent(
        event_id=event_id,
        payload=sample_mdd_payload(metadata={"mdd_stage": stage, "target_cash_percent": "50"}),
        created_at=NOW,
    )


def sample_review_input(
    *,
    review_id: str = "review-2026-q1",
    calendar_days: int | None = None,
    nav_snapshots: tuple[NavSnapshot, ...] | None = None,
    **overrides: object,
) -> PaperReviewInput:
    period = sample_review_period(calendar_days=calendar_days)
    base: dict[str, object] = {
        "review_id": review_id,
        "created_at": NOW,
        "period": period,
        "nav_snapshots": nav_snapshots or sample_nav_series(count=10),
    }
    if "postmortem_records" not in overrides:
        if calendar_days is None or calendar_days >= 90:
            base["postmortem_records"] = (sample_postmortem_record(),)
        else:
            base["postmortem_records"] = ()
    base.update(overrides)
    return PaperReviewInput(**base)
