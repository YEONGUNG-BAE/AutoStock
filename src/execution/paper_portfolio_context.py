"""RTM-7a: canonical paper portfolio context service.

SQLiteLedger(현금/포지션 truth) + 최신 시장 스냅샷(quote)에서 RiskFilter 가 요구하는
canonical RiskFilterContext 를 *재구성 없이 계산*한다. 이 계층은 network/LLM/broker 를
호출하지 않으며, 어떤 영속 상태도 변경하지 않는다(순수 read + compute).

밸류에이션 정책(v1):
- 기준 계좌 = AccountRole.PAPER, 기준 통화 = KRW. 비-KRW cash/position/quote 는 FX 추정
  없이 currency_unsupported 로 fail-closed.
- cash 는 ledger current_cash(KRW, PAPER). 없으면 cash_missing, as_of 가 now 보다
  미래면 cash_future. cash 는 정당한 projection 이므로 as_of != now 자체는 거부 사유가 아니다.
- 포트폴리오 MARK = quote midpoint (bid+ask)/2. 동일 (symbol, market) 스냅샷이
  evaluated_at == now, quote 존재 + quote_fresh 여야 하며 trade fallback 은 없다.
  missing/stale/identity-mismatch 는 모두 fail-closed.
- 실행 가격(proposed_price, BUY=ask/SELL=bid)은 coordinator 가 결정해 주입한다. 실행
  가격과 포트폴리오 mark 는 *섞지 않는다*: mark 는 valuation, proposed_price 는 slippage 용.

계산:
- position market_value = quantity × midpoint mark
- cumulative_buy_cost = quantity × avg_cost
- invested_amount = Σ position market_value (CASH asset_class 제외)
- total_nav = cash + invested_amount (nav <= 0 → nav_invalid)
- current_symbol_market_value = qty × mark (포지션 없으면 0)
- current_symbol_cumulative_buy_cost = qty × avg_cost (없으면 0)
- current_symbol_weight_percent = value / total_nav × 100
- asset weights: AssetClass bucket(KR_EQUITY→kr, US_EQUITY→us, GOLD→gold; CASH 제외;
  그 외 → asset_bucket_unsupported), denominator = invested_amount, invested==0 → 전부 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from allocator.models import AssetBucket
from domain._datetime import require_timezone_aware_datetime
from domain.enums import AccountRole, AssetClass, Currency, Market
from domain.market import MarketPrice
from domain.money import Money
from domain.identifiers import Percent
from domain.position import CashSnapshot, Position
from market_data.latest_state import LatestMarketStateSnapshot
from risk.models import AssetClassWeights, RiskFilterContext, RiskMode

# v1 canonical 기준 계좌/통화.
BASE_ACCOUNT_ROLE = AccountRole.PAPER
BASE_CURRENCY = Currency.KRW

# --- typed fail-closed reason codes (broker/engine 호출 전 차단) ---
REASON_CASH_MISSING = "portfolio_cash_missing"
REASON_CASH_FUTURE = "portfolio_cash_future"
REASON_POSITION_SOURCE_ERROR = "portfolio_position_source_error"
REASON_SNAPSHOT_MISSING = "portfolio_snapshot_missing"
REASON_SNAPSHOT_STALE = "portfolio_snapshot_stale"
REASON_SNAPSHOT_IDENTITY_MISMATCH = "portfolio_snapshot_identity_mismatch"
REASON_CURRENCY_UNSUPPORTED = "portfolio_currency_unsupported"
REASON_ASSET_BUCKET_UNSUPPORTED = "portfolio_asset_bucket_unsupported"
REASON_NAV_INVALID = "portfolio_nav_invalid"
REASON_PROPOSED_PRICE_MISMATCH = "portfolio_proposed_price_mismatch"


class PaperPortfolioContextError(Exception):
    """canonical context 계산 단계의 typed fail-closed 사유."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code


# AssetClass → AssetClassWeights bucket. CASH 는 invested 에서 제외된다.
_ASSET_BUCKET: dict[AssetClass, str] = {
    AssetClass.KR_EQUITY: "kr",
    AssetClass.US_EQUITY: "us",
    AssetClass.GOLD: "gold",
}


@runtime_checkable
class PaperPortfolioLedgerSource(Protocol):
    """현금/포지션 truth 조회 계약. SQLiteLedger 가 그대로 만족한다."""

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot | None: ...

    def list_positions(self) -> tuple[Position, ...]: ...


@runtime_checkable
class PortfolioMarketStateSource(Protocol):
    """(symbol, market) 최신 스냅샷 조회 계약. LatestMarketStateStore 어댑터가 만족한다."""

    def get_snapshot(
        self, symbol: str, market: Market, *, now: datetime
    ) -> LatestMarketStateSnapshot | None: ...


@dataclass(frozen=True)
class PaperPortfolioPolicy:
    """valuation 외에 RiskFilterContext 가 요구하는 정책/모드 입력.

    ledger/market 에서 유도할 수 없는 값(운용 모드, allocator 목표, gold 카운트 등)만
    담는다. NAV/cash/invested/weight 는 절대 여기서 받지 않는다(주입 금지).
    """

    mode: RiskMode
    allocator_tolerance_percent: Percent = field(default_factory=lambda: Percent("5"))
    allocator_symbol_target_weight: Percent | None = None
    paper_observation_min_invested_percent: Percent | None = None
    mdd_percent: Percent | None = None
    gold_trades_this_month: int = 0
    gold_trades_this_quarter: int = 0
    asset_bucket: AssetBucket | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaperPortfolioValuation:
    """canonical valuation 불변 스냅샷. risk_filter_context 가 최종 산출물이다."""

    created_at: datetime
    base_currency: Currency
    cash: Money
    invested_amount: Money
    total_nav: Money
    current_symbol_market_value: Money
    current_symbol_cumulative_buy_cost: Money
    current_symbol_weight_percent: Percent
    current_asset_weights: AssetClassWeights
    position_quantity: Decimal
    marks: Mapping[str, MarketPrice]
    risk_filter_context: RiskFilterContext


class PaperPortfolioContextService:
    """ledger + 시장 스냅샷에서 canonical RiskFilterContext 를 계산하는 paper-only 서비스.

    constructor 는 의존성 주입만 한다(FS 경로 하드코딩/DB write 금지). 모든 실패는
    PaperPortfolioContextError(reason_code) 로 던진다 — raw 예외를 흘리지 않는다.
    """

    def __init__(
        self,
        *,
        ledger_source: PaperPortfolioLedgerSource,
        market_state_source: PortfolioMarketStateSource,
    ) -> None:
        self._ledger = ledger_source
        self._market = market_state_source

    def build_context(
        self,
        *,
        symbol: str,
        market: Market,
        proposed_price: MarketPrice,
        policy: PaperPortfolioPolicy,
        now: datetime,
    ) -> PaperPortfolioValuation:
        aware_now = require_timezone_aware_datetime(now, field_name="now")

        # 실행 가격(proposed_price)은 평가 중인 종목과 동일해야 하고 v1 KRW 여야 한다.
        if proposed_price.symbol != symbol or proposed_price.market != market:
            raise PaperPortfolioContextError(
                REASON_PROPOSED_PRICE_MISMATCH,
                "proposed_price identity differs from evaluated symbol/market.",
            )
        if proposed_price.currency != BASE_CURRENCY:
            raise PaperPortfolioContextError(
                REASON_CURRENCY_UNSUPPORTED,
                f"proposed_price currency {proposed_price.currency.value} is not {BASE_CURRENCY.value}.",
            )

        cash = self._load_cash(aware_now)
        positions = self._load_positions()

        marks: dict[str, MarketPrice] = {}
        invested_total = Decimal("0")
        bucket_values: dict[str, Decimal] = {"kr": Decimal("0"), "us": Decimal("0"), "gold": Decimal("0")}

        # 현재 평가 종목의 mark 는 포지션 유무와 무관하게 항상 필요하다(slippage 기준).
        current_mark = self._mark_for(symbol, market, now=aware_now)
        marks[symbol] = current_mark

        current_quantity = Decimal("0")
        current_avg_cost = Decimal("0")

        for position in positions:
            if position.currency != BASE_CURRENCY:
                raise PaperPortfolioContextError(
                    REASON_CURRENCY_UNSUPPORTED,
                    f"position {position.symbol} currency {position.currency.value} is not "
                    f"{BASE_CURRENCY.value}.",
                )
            if position.asset_class is AssetClass.CASH:
                # CASH asset_class 포지션은 invested/weight 에서 제외한다(현금은 get_cash truth).
                continue
            bucket = _ASSET_BUCKET.get(position.asset_class)
            if bucket is None:
                raise PaperPortfolioContextError(
                    REASON_ASSET_BUCKET_UNSUPPORTED,
                    f"asset_class {position.asset_class.value} has no canonical bucket.",
                )
            mark = marks.get(position.symbol)
            if mark is None:
                mark = self._mark_for(position.symbol, position.market, now=aware_now)
                marks[position.symbol] = mark
            market_value = position.quantity * mark.price
            invested_total += market_value
            bucket_values[bucket] += market_value
            if position.symbol == symbol and position.market == market:
                current_quantity = position.quantity
                current_avg_cost = position.avg_cost

        invested_amount = Money(amount=invested_total, currency=BASE_CURRENCY)
        total_nav_amount = cash.amount + invested_total
        if total_nav_amount <= 0:
            raise PaperPortfolioContextError(
                REASON_NAV_INVALID, "total_nav must be greater than 0."
            )
        total_nav = Money(amount=total_nav_amount, currency=BASE_CURRENCY)

        current_symbol_value_amount = current_quantity * current_mark.price
        current_symbol_market_value = Money(
            amount=current_symbol_value_amount, currency=BASE_CURRENCY
        )
        current_symbol_cumulative_buy_cost = Money(
            amount=current_quantity * current_avg_cost, currency=BASE_CURRENCY
        )
        current_symbol_weight_percent = Percent(
            current_symbol_value_amount / total_nav_amount * Decimal("100")
        )

        asset_weights = _asset_weights(bucket_values, invested_total)

        proposed_money = Money(amount=proposed_price.price, currency=proposed_price.currency)

        context = RiskFilterContext(
            created_at=aware_now,
            mode=policy.mode,
            total_nav=total_nav,
            cash=Money(amount=cash.amount, currency=BASE_CURRENCY),
            invested_amount=invested_amount,
            current_symbol_market_value=current_symbol_market_value,
            current_symbol_cumulative_buy_cost=current_symbol_cumulative_buy_cost,
            current_symbol_weight_percent=current_symbol_weight_percent,
            current_asset_weights=asset_weights,
            allocator_tolerance_percent=policy.allocator_tolerance_percent,
            allocator_symbol_target_weight=policy.allocator_symbol_target_weight,
            paper_observation_min_invested_percent=policy.paper_observation_min_invested_percent,
            mdd_percent=policy.mdd_percent,
            market=market,
            currency=BASE_CURRENCY,
            asset_bucket=policy.asset_bucket,
            gold_trades_this_month=policy.gold_trades_this_month,
            gold_trades_this_quarter=policy.gold_trades_this_quarter,
            proposed_price=proposed_money,
            reference_prices=dict(marks),
            metadata=dict(policy.metadata),
        )

        return PaperPortfolioValuation(
            created_at=aware_now,
            base_currency=BASE_CURRENCY,
            cash=Money(amount=cash.amount, currency=BASE_CURRENCY),
            invested_amount=invested_amount,
            total_nav=total_nav,
            current_symbol_market_value=current_symbol_market_value,
            current_symbol_cumulative_buy_cost=current_symbol_cumulative_buy_cost,
            current_symbol_weight_percent=current_symbol_weight_percent,
            current_asset_weights=asset_weights,
            position_quantity=current_quantity,
            marks=dict(marks),
            risk_filter_context=context,
        )

    # ------------------------------------------------------------------ internals
    def _load_cash(self, now: datetime) -> CashSnapshot:
        try:
            cash = self._ledger.get_cash(BASE_CURRENCY, BASE_ACCOUNT_ROLE)
        except Exception as exc:  # noqa: BLE001 - typed fail-closed, no raw leak
            raise PaperPortfolioContextError(
                REASON_POSITION_SOURCE_ERROR, "ledger get_cash raised."
            ) from exc
        if cash is None:
            raise PaperPortfolioContextError(
                REASON_CASH_MISSING,
                f"No cash for currency={BASE_CURRENCY.value} role={BASE_ACCOUNT_ROLE.value}.",
            )
        if cash.currency != BASE_CURRENCY:
            raise PaperPortfolioContextError(
                REASON_CURRENCY_UNSUPPORTED,
                f"cash currency {cash.currency.value} is not {BASE_CURRENCY.value}.",
            )
        # cash 는 정당한 projection 이므로 as_of != now 는 거부하지 않는다. 단 미래는 fail-closed.
        if cash.as_of > now:
            raise PaperPortfolioContextError(
                REASON_CASH_FUTURE, "cash as_of is in the future."
            )
        return cash

    def _load_positions(self) -> tuple[Position, ...]:
        try:
            positions = self._ledger.list_positions()
        except Exception as exc:  # noqa: BLE001 - typed fail-closed, no raw leak
            raise PaperPortfolioContextError(
                REASON_POSITION_SOURCE_ERROR, "ledger list_positions raised."
            ) from exc
        return tuple(p for p in positions if p.account_role is BASE_ACCOUNT_ROLE)

    def _mark_for(self, symbol: str, market: Market, *, now: datetime) -> MarketPrice:
        try:
            snapshot = self._market.get_snapshot(symbol, market, now=now)
        except Exception as exc:  # noqa: BLE001 - typed fail-closed, no raw leak
            raise PaperPortfolioContextError(
                REASON_POSITION_SOURCE_ERROR, "market_state get_snapshot raised."
            ) from exc
        if snapshot is None or snapshot.quote is None:
            raise PaperPortfolioContextError(
                REASON_SNAPSHOT_MISSING,
                f"No quote snapshot for symbol={symbol} market={market.value}.",
            )
        if snapshot.symbol != symbol or snapshot.market != market:
            raise PaperPortfolioContextError(
                REASON_SNAPSHOT_IDENTITY_MISMATCH,
                "snapshot identity differs from requested symbol/market.",
            )
        if snapshot.evaluated_at != now or not snapshot.quote_fresh:
            raise PaperPortfolioContextError(
                REASON_SNAPSHOT_STALE,
                f"quote snapshot for symbol={symbol} market={market.value} is stale.",
            )
        quote = snapshot.quote
        if quote.currency != BASE_CURRENCY:
            raise PaperPortfolioContextError(
                REASON_CURRENCY_UNSUPPORTED,
                f"quote currency {quote.currency.value} is not {BASE_CURRENCY.value}.",
            )
        midpoint = (quote.bid_price + quote.ask_price) / Decimal("2")
        return MarketPrice(
            symbol=symbol,
            market=market,
            currency=BASE_CURRENCY,
            price=midpoint,
            as_of=quote.quote_at,
        )


def _asset_weights(bucket_values: Mapping[str, Decimal], invested_total: Decimal) -> AssetClassWeights:
    if invested_total <= 0:
        zero = Percent("0")
        return AssetClassWeights(kr=zero, us=zero, gold=zero)
    return AssetClassWeights(
        kr=Percent(bucket_values["kr"] / invested_total * Decimal("100")),
        us=Percent(bucket_values["us"] / invested_total * Decimal("100")),
        gold=Percent(bucket_values["gold"] / invested_total * Decimal("100")),
    )


__all__ = [
    "BASE_ACCOUNT_ROLE",
    "BASE_CURRENCY",
    "PaperPortfolioContextError",
    "PaperPortfolioContextService",
    "PaperPortfolioLedgerSource",
    "PaperPortfolioPolicy",
    "PaperPortfolioValuation",
    "PortfolioMarketStateSource",
    "REASON_ASSET_BUCKET_UNSUPPORTED",
    "REASON_CASH_FUTURE",
    "REASON_CASH_MISSING",
    "REASON_CURRENCY_UNSUPPORTED",
    "REASON_NAV_INVALID",
    "REASON_POSITION_SOURCE_ERROR",
    "REASON_PROPOSED_PRICE_MISMATCH",
    "REASON_SNAPSHOT_IDENTITY_MISMATCH",
    "REASON_SNAPSHOT_MISSING",
    "REASON_SNAPSHOT_STALE",
]
