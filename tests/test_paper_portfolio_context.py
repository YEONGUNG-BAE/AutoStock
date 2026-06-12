"""RTM-7a: PaperPortfolioContextService canonical valuation 단위 테스트.

ledger truth(cash/positions) + 최신 스냅샷(midpoint mark)에서 RiskFilterContext 를
*재구성 없이* 계산하는지, 그리고 모든 fail-closed 경로가 typed reason_code 로 닫히는지
검증한다. network/LLM/broker 없음. 모든 입력은 fake source 다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain import Currency, Money, Percent
from domain.enums import AccountRole, AssetClass, Market
from domain.market import MarketPrice
from domain.position import CashSnapshot, Position
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.models import NormalizedBestBidAsk, NormalizedTradeTick, ProviderSequence
from risk.models import RiskMode

from execution.paper_portfolio_context import (
    REASON_CASH_FUTURE,
    REASON_CASH_MISSING,
    REASON_CURRENCY_UNSUPPORTED,
    REASON_NAV_INVALID,
    REASON_POSITION_SOURCE_ERROR,
    REASON_PROPOSED_PRICE_MISMATCH,
    REASON_SNAPSHOT_IDENTITY_MISMATCH,
    REASON_SNAPSHOT_MISSING,
    REASON_SNAPSHOT_STALE,
    PaperPortfolioContextError,
    PaperPortfolioContextService,
    PaperPortfolioPolicy,
)

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
KRW = Currency.KRW


# ------------------------------------------------------------------ builders
def _quote(
    *,
    symbol: str = "005930",
    market: Market = Market.KR,
    bid: str = "70000",
    ask: str = "70000",
    currency: Currency = KRW,
    at: datetime = NOW,
) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis", symbol=symbol, market=market, currency=currency,
        bid_price=bid, ask_price=ask, bid_quantity="10", ask_quantity="10",
        quote_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=at),
    )


def _trade(*, symbol: str = "005930", market: Market = Market.KR, at: datetime = NOW) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis", symbol=symbol, market=market, currency=KRW,
        price="70000", quantity="1", trade_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=at),
    )


def _snap(
    *,
    symbol: str = "005930",
    market: Market = Market.KR,
    quote: NormalizedBestBidAsk | None = None,
    quote_fresh: bool = True,
    evaluated_at: datetime = NOW,
    with_quote: bool = True,
) -> LatestMarketStateSnapshot:
    return LatestMarketStateSnapshot(
        market=market,
        symbol=symbol,
        trade=_trade(symbol=symbol, market=market, at=evaluated_at),
        quote=(quote if quote is not None else _quote(symbol=symbol, market=market, at=evaluated_at)) if with_quote else None,
        trade_fresh=True,
        quote_fresh=quote_fresh,
        evaluated_at=evaluated_at,
    )


def _cash(amount: str = "100000000", *, currency: Currency = KRW, as_of: datetime = NOW) -> CashSnapshot:
    return CashSnapshot(currency=currency, amount=Decimal(amount), account_role=AccountRole.PAPER, as_of=as_of)


def _position(
    *,
    symbol: str = "005930",
    market: Market = Market.KR,
    asset_class: AssetClass = AssetClass.KR_EQUITY,
    quantity: str = "100",
    avg_cost: str = "60000",
    currency: Currency = KRW,
    account_role: AccountRole = AccountRole.PAPER,
) -> Position:
    return Position(
        symbol=symbol, market=market, asset_class=asset_class, account_role=account_role,
        quantity=Decimal(quantity), avg_cost=Decimal(avg_cost), currency=currency,
    )


def _proposed(*, symbol: str = "005930", market: Market = Market.KR, price: str = "70000", currency: Currency = KRW, as_of: datetime = NOW) -> MarketPrice:
    return MarketPrice(symbol=symbol, market=market, currency=currency, price=Decimal(price), as_of=as_of)


@dataclass
class _FakeLedger:
    cash: CashSnapshot | None = field(default_factory=_cash)
    positions: tuple[Position, ...] = ()
    raise_cash: bool = False
    raise_positions: bool = False

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot | None:
        if self.raise_cash:
            raise RuntimeError("ledger get_cash failed")
        return self.cash

    def list_positions(self) -> tuple[Position, ...]:
        if self.raise_positions:
            raise RuntimeError("ledger list_positions failed")
        return self.positions


@dataclass
class _FakeMarket:
    snapshots: dict[tuple[str, Market], LatestMarketStateSnapshot | None] = field(default_factory=dict)
    raise_get: bool = False

    def get_snapshot(self, symbol: str, market: Market, *, now: datetime) -> LatestMarketStateSnapshot | None:
        if self.raise_get:
            raise RuntimeError("market get_snapshot failed")
        if (symbol, market) in self.snapshots:
            return self.snapshots[(symbol, market)]
        return _snap(symbol=symbol, market=market, evaluated_at=now)


def _service(ledger: _FakeLedger, market: _FakeMarket) -> PaperPortfolioContextService:
    return PaperPortfolioContextService(ledger_source=ledger, market_state_source=market)


def _policy(**overrides: object) -> PaperPortfolioPolicy:
    base: dict[str, object] = {"mode": RiskMode.REBALANCING}
    base.update(overrides)
    return PaperPortfolioPolicy(**base)  # type: ignore[arg-type]


# ============================================================= happy-path valuation
def test_cash_only_no_position_nav_equals_cash_invested_zero() -> None:
    svc = _service(_FakeLedger(cash=_cash("100000000")), _FakeMarket())
    val = svc.build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    assert val.cash.amount == Decimal("100000000")
    assert val.invested_amount.amount == Decimal("0")
    assert val.total_nav.amount == Decimal("100000000")
    assert val.current_symbol_market_value.amount == Decimal("0")
    assert val.current_symbol_cumulative_buy_cost.amount == Decimal("0")
    assert val.current_symbol_weight_percent == Percent("0")
    assert val.position_quantity == Decimal("0")
    assert val.current_asset_weights.kr == Percent("0")
    assert val.current_asset_weights.us == Percent("0")
    assert val.current_asset_weights.gold == Percent("0")
    ctx = val.risk_filter_context
    assert ctx.created_at == NOW
    assert ctx.mode is RiskMode.REBALANCING
    assert ctx.currency is KRW
    assert ctx.market is Market.KR


def test_single_kr_position_invested_nav_cumulative_weight_exact() -> None:
    # midpoint=(69000+71000)/2=70000; 100주 → market_value 7M, cumulative 100*60000=6M.
    quote = _quote(bid="69000", ask="71000")
    snap = _snap(quote=quote)
    ledger = _FakeLedger(cash=_cash("93000000"), positions=(_position(quantity="100", avg_cost="60000"),))
    val = _service(ledger, _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(price="71000"), current_snapshot=snap, policy=_policy(), now=NOW
    )
    assert val.invested_amount.amount == Decimal("7000000")
    assert val.total_nav.amount == Decimal("100000000")  # 93M + 7M
    assert val.current_symbol_market_value.amount == Decimal("7000000")
    assert val.current_symbol_cumulative_buy_cost.amount == Decimal("6000000")
    assert val.current_symbol_weight_percent == Percent("7")
    assert val.position_quantity == Decimal("100")
    assert val.marks[(Market.KR, "005930")].price == Decimal("70000")


def test_multiple_positions_invested_sum_and_asset_buckets() -> None:
    # 005930(KR) 100주@70000 mark = 7M; AAPL...gold 대신 GOLD ETF 50주@40000 mark = 2M.
    market = _FakeMarket(
        snapshots={
            ("411060", Market.KR): _snap(symbol="411060", quote=_quote(symbol="411060", bid="40000", ask="40000")),
        }
    )
    ledger = _FakeLedger(
        cash=_cash("91000000"),
        positions=(
            _position(symbol="005930", quantity="100", avg_cost="60000", asset_class=AssetClass.KR_EQUITY),
            _position(symbol="411060", quantity="50", avg_cost="38000", asset_class=AssetClass.GOLD),
        ),
    )
    val = _service(ledger, market).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(),
        current_snapshot=_snap(quote=_quote(bid="70000", ask="70000")), policy=_policy(), now=NOW
    )
    assert val.invested_amount.amount == Decimal("9000000")  # 7M + 2M
    assert val.total_nav.amount == Decimal("100000000")  # 91M + 9M
    # KR bucket = 7M/9M, GOLD = 2M/9M.
    assert val.current_asset_weights.kr.value == (Decimal("7000000") / Decimal("9000000") * Decimal("100"))
    assert val.current_asset_weights.gold.value == (Decimal("2000000") / Decimal("9000000") * Decimal("100"))
    assert val.current_asset_weights.us == Percent("0")


def test_midpoint_mark_independent_of_buy_sell_proposed_price() -> None:
    quote = _quote(bid="69000", ask="71000")  # midpoint 70000
    snap = _snap(quote=quote)
    ledger = _FakeLedger(cash=_cash("93000000"), positions=(_position(quantity="100", avg_cost="60000"),))
    buy_val = _service(ledger, _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(price="71000"), current_snapshot=snap, policy=_policy(), now=NOW
    )
    sell_val = _service(ledger, _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(price="69000"), current_snapshot=snap, policy=_policy(), now=NOW
    )
    # 실행가격(ask vs bid)이 달라도 NAV/invested/mark 는 midpoint 기준이라 동일하다.
    assert buy_val.total_nav.amount == sell_val.total_nav.amount == Decimal("100000000")
    assert buy_val.marks[(Market.KR, "005930")].price == sell_val.marks[(Market.KR, "005930")].price == Decimal("70000")
    # proposed_price 만 다르다(slippage 기준).
    assert buy_val.risk_filter_context.proposed_price.amount == Decimal("71000")
    assert sell_val.risk_filter_context.proposed_price.amount == Decimal("69000")


def test_invested_zero_asset_weights_all_zero() -> None:
    val = _service(_FakeLedger(cash=_cash("50000000")), _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    assert val.current_asset_weights.kr == Percent("0")
    assert val.current_asset_weights.us == Percent("0")
    assert val.current_asset_weights.gold == Percent("0")


def test_valuation_is_frozen() -> None:
    val = _service(_FakeLedger(), _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    with pytest.raises(Exception):
        val.total_nav = Money.from_str("1", KRW)  # type: ignore[misc]


def test_valuation_marks_mapping_is_read_only() -> None:
    val = _service(_FakeLedger(), _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    with pytest.raises(TypeError):
        val.marks[(Market.KR, "005930")] = _proposed()  # type: ignore[index]


def test_policy_metadata_is_read_only() -> None:
    policy = _policy(metadata={"k": "v"})
    assert policy.metadata["k"] == "v"
    with pytest.raises(TypeError):
        policy.metadata["k2"] = "v2"  # type: ignore[index]


def test_policy_metadata_is_deeply_immutable() -> None:
    # 중첩된 dict/list 값까지 변이 불가능해야 한다(얕은 MappingProxyType 만으로는 부족).
    nested = {"d": {"a": 1}, "l": [1, 2]}
    policy = _policy(metadata=nested)
    assert policy.metadata["d"]["a"] == 1
    assert policy.metadata["l"] == (1, 2)
    with pytest.raises(TypeError):
        policy.metadata["d"]["a"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.metadata["l"][0] = 99  # type: ignore[index]


def test_policy_metadata_does_not_alias_source() -> None:
    # 원본을 이후 변이해도 정책 스냅샷에 영향이 없어야 한다(deep copy on freeze).
    source = {"d": {"a": 1}}
    policy = _policy(metadata=source)
    source["d"]["a"] = 99
    source["new"] = "x"
    assert policy.metadata["d"]["a"] == 1
    assert "new" not in policy.metadata


def test_marks_keyed_by_market_symbol_and_reference_prices_symbol_keyed() -> None:
    # 내부 marks 는 (market, symbol) 튜플 키, RiskFilter.reference_prices 는 symbol 단독 키.
    val = _service(_FakeLedger(), _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    assert (Market.KR, "005930") in val.marks
    refs = val.risk_filter_context.reference_prices
    assert refs is not None
    assert "005930" in refs
    assert refs["005930"].price == Decimal("70000")


def test_target_mark_uses_current_snapshot_not_market_source() -> None:
    # market source 가 005930 에 대해 전혀 다른 quote(midpoint 50000)를 줘도, 대상 종목 mark 는
    # coordinator 가 넘긴 current_snapshot(midpoint 70000)에서 계산되어야 한다.
    stray = _FakeMarket(snapshots={("005930", Market.KR): _snap(quote=_quote(bid="50000", ask="50000"))})
    val = _service(_FakeLedger(), stray).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(),
        current_snapshot=_snap(quote=_quote(bid="70000", ask="70000")), policy=_policy(), now=NOW
    )
    assert val.marks[(Market.KR, "005930")].price == Decimal("70000")


def test_proposed_as_of_must_match_snapshot_quote_at() -> None:
    # proposed_price 가 current_snapshot 의 quote 에서 파생되지 않으면(as_of 불일치) fail-closed.
    val_svc = _service(_FakeLedger(), _FakeMarket())
    with pytest.raises(PaperPortfolioContextError) as exc:
        val_svc.build_context(
            symbol="005930", market=Market.KR,
            proposed_price=_proposed(as_of=NOW - timedelta(seconds=3)),
            current_snapshot=_snap(), policy=_policy(), now=NOW,
        )
    assert exc.value.reason_code == REASON_PROPOSED_PRICE_MISMATCH


# ============================================================= fail-closed paths
def _expect(svc: PaperPortfolioContextService, reason: str, **kwargs: object) -> None:
    call: dict[str, object] = {
        "symbol": "005930", "market": Market.KR, "proposed_price": _proposed(),
        "current_snapshot": _snap(), "policy": _policy(), "now": NOW,
    }
    call.update(kwargs)
    with pytest.raises(PaperPortfolioContextError) as exc:
        svc.build_context(**call)  # type: ignore[arg-type]
    assert exc.value.reason_code == reason


def test_cash_missing_fails_closed() -> None:
    _expect(_service(_FakeLedger(cash=None), _FakeMarket()), REASON_CASH_MISSING)


def test_cash_future_fails_closed() -> None:
    svc = _service(_FakeLedger(cash=_cash("100000000", as_of=NOW + timedelta(seconds=1))), _FakeMarket())
    _expect(svc, REASON_CASH_FUTURE)


def test_cash_non_krw_fails_closed() -> None:
    svc = _service(_FakeLedger(cash=_cash("100000000", currency=Currency.USD)), _FakeMarket())
    _expect(svc, REASON_CURRENCY_UNSUPPORTED)


def test_snapshot_quote_none_fails_closed() -> None:
    # 대상 종목 mark 는 current_snapshot 에서 계산되므로, quote 가 없으면 SNAPSHOT_MISSING.
    _expect(_service(_FakeLedger(), _FakeMarket()), REASON_SNAPSHOT_MISSING, current_snapshot=_snap(with_quote=False))


def test_snapshot_stale_fails_closed() -> None:
    _expect(_service(_FakeLedger(), _FakeMarket()), REASON_SNAPSHOT_STALE, current_snapshot=_snap(quote_fresh=False))


def test_snapshot_evaluated_at_mismatch_fails_closed() -> None:
    _expect(
        _service(_FakeLedger(), _FakeMarket()), REASON_SNAPSHOT_STALE,
        current_snapshot=_snap(evaluated_at=NOW - timedelta(seconds=5)),
    )


def test_snapshot_identity_mismatch_fails_closed() -> None:
    _expect(
        _service(_FakeLedger(), _FakeMarket()), REASON_SNAPSHOT_IDENTITY_MISMATCH,
        current_snapshot=_snap(symbol="000660"),
    )


def test_quote_non_krw_fails_closed() -> None:
    _expect(
        _service(_FakeLedger(), _FakeMarket()), REASON_CURRENCY_UNSUPPORTED,
        current_snapshot=_snap(quote=_quote(currency=Currency.USD)),
    )


def test_held_position_snapshot_stale_fails_closed() -> None:
    # 보유 중인 다른 종목(411060)의 market-source mark 가 stale 이면 build 전체가 fail-closed.
    market = _FakeMarket(snapshots={("411060", Market.KR): _snap(symbol="411060", quote_fresh=False)})
    ledger = _FakeLedger(
        cash=_cash("100000000"),
        positions=(_position(symbol="411060", quantity="50", avg_cost="38000", asset_class=AssetClass.GOLD),),
    )
    _expect(_service(ledger, market), REASON_SNAPSHOT_STALE)


def test_position_non_krw_fails_closed() -> None:
    ledger = _FakeLedger(
        cash=_cash("100000000"),
        positions=(_position(symbol="AAPL", market=Market.US, asset_class=AssetClass.US_EQUITY, currency=Currency.USD),),
    )
    _expect(_service(ledger, _FakeMarket()), REASON_CURRENCY_UNSUPPORTED)


def test_nav_invalid_fails_closed() -> None:
    # cash 0 + 포지션 없음 → nav 0 → nav_invalid.
    _expect(_service(_FakeLedger(cash=_cash("0")), _FakeMarket()), REASON_NAV_INVALID)


def test_proposed_price_identity_mismatch_fails_closed() -> None:
    _expect(_service(_FakeLedger(), _FakeMarket()), REASON_PROPOSED_PRICE_MISMATCH, proposed_price=_proposed(symbol="000660"))


def test_proposed_price_non_krw_fails_closed() -> None:
    _expect(
        _service(_FakeLedger(), _FakeMarket()),
        REASON_CURRENCY_UNSUPPORTED,
        proposed_price=MarketPrice(symbol="005930", market=Market.KR, currency=Currency.USD, price=Decimal("70000"), as_of=NOW),
    )


def test_ledger_cash_raise_fails_closed() -> None:
    _expect(_service(_FakeLedger(raise_cash=True), _FakeMarket()), REASON_POSITION_SOURCE_ERROR)


def test_ledger_positions_raise_fails_closed() -> None:
    _expect(_service(_FakeLedger(raise_positions=True), _FakeMarket()), REASON_POSITION_SOURCE_ERROR)


def test_market_source_raise_fails_closed() -> None:
    # 대상 종목은 current_snapshot 으로 처리되므로, market source 는 보유 종목이 있을 때만 호출된다.
    ledger = _FakeLedger(
        cash=_cash("100000000"),
        positions=(_position(symbol="411060", quantity="50", avg_cost="38000", asset_class=AssetClass.GOLD),),
    )
    _expect(_service(ledger, _FakeMarket(raise_get=True)), REASON_POSITION_SOURCE_ERROR)


def test_non_paper_positions_excluded() -> None:
    # 다른 계좌(US_REGULAR) 포지션은 PAPER valuation 에서 제외된다.
    ledger = _FakeLedger(
        cash=_cash("100000000"),
        positions=(_position(symbol="000660", quantity="100", avg_cost="50000", account_role=AccountRole.US_REGULAR),),
    )
    val = _service(ledger, _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    assert val.invested_amount.amount == Decimal("0")
    assert val.total_nav.amount == Decimal("100000000")


def test_cash_asset_class_position_excluded_from_invested() -> None:
    # CASH asset_class 포지션은 invested/weight 에서 제외된다(현금은 get_cash truth).
    ledger = _FakeLedger(
        cash=_cash("100000000"),
        positions=(_position(symbol="CASHX", quantity="100", avg_cost="1", asset_class=AssetClass.CASH),),
    )
    val = _service(ledger, _FakeMarket()).build_context(
        symbol="005930", market=Market.KR, proposed_price=_proposed(), current_snapshot=_snap(), policy=_policy(), now=NOW
    )
    assert val.invested_amount.amount == Decimal("0")
    assert val.total_nav.amount == Decimal("100000000")
