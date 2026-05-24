from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from broker.kis_client import KisReadOnlyClient
from broker.kis_live_adapter import KisLiveOrderBlockedError, KisLiveReadOnlyBrokerAdapter
from broker.kis_models import KisAccountRoleError
from broker.kis_transport import KisHttpResponse
from config.settings import BrokerAccountRoleSettings, KisLiveSettings, KisReadOnlySettings
from domain.enums import AccountRole, AssetClass, Currency, Market, OrderSide, OrderType
from domain.market import MarketPrice
from config.settings import ExecutionMode
from domain.order import OrderIntent
from kis_fake_transport import FakeKisTransport


def _make_adapter(environ: dict[str, str] | None = None) -> KisLiveReadOnlyBrokerAdapter:
    transport = FakeKisTransport()
    client = KisReadOnlyClient(
        live_settings=KisLiveSettings(),
        account_role_settings=BrokerAccountRoleSettings(),
        read_only_settings=KisReadOnlySettings(),
        transport=transport,
        environ={
            "KIS_LIVE_APP_KEY": "app-key",
            "KIS_LIVE_APP_SECRET": "app-secret",
            "KIS_ISA_ACCOUNT": "1234567801",
            "KIS_US_REGULAR_ACCOUNT": "8765432101",
            **(environ or {}),
        },
    )
    return KisLiveReadOnlyBrokerAdapter(client)


def _manual_intent(**overrides) -> OrderIntent:
    defaults = {
        "order_id": "ord-1",
        "correlation_id": "corr-1",
        "symbol": "005930",
        "market": Market.KR,
        "asset_class": AssetClass.KR_EQUITY,
        "account_role": AccountRole.KR_TAX_ADVANTAGED,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "execution_mode": ExecutionMode.MANUAL,
        "quantity": Decimal("1"),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def test_get_cash_returns_cash_snapshot() -> None:
    adapter = _make_adapter()
    cash = adapter.get_cash(Currency.KRW, AccountRole.KR_TAX_ADVANTAGED)

    assert cash.currency == Currency.KRW
    assert cash.amount == Decimal("5000000")
    assert cash.account_role == AccountRole.KR_TAX_ADVANTAGED


def test_get_position_returns_position() -> None:
    adapter = _make_adapter()
    position = adapter.get_position("005930", Market.KR, AccountRole.KR_TAX_ADVANTAGED)

    assert position is not None
    assert position.symbol == "005930"
    assert position.quantity == Decimal("10")


def test_get_position_returns_none_when_missing() -> None:
    adapter = _make_adapter()
    position = adapter.get_position("000660", Market.KR, AccountRole.KR_TAX_ADVANTAGED)

    assert position is None


def test_list_positions_returns_tuple() -> None:
    adapter = _make_adapter()
    positions = adapter.list_positions()

    assert isinstance(positions, tuple)
    assert len(positions) >= 1


def test_submit_order_fails_closed() -> None:
    adapter = _make_adapter()
    intent = _manual_intent()
    market_price = MarketPrice(
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal("71000"),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(KisLiveOrderBlockedError, match="does not submit orders"):
        adapter.submit_order(intent, market_price)


def test_submit_order_makes_zero_transport_calls() -> None:
    transport = FakeKisTransport()
    client = KisReadOnlyClient(
        live_settings=KisLiveSettings(),
        account_role_settings=BrokerAccountRoleSettings(),
        read_only_settings=KisReadOnlySettings(),
        transport=transport,
        environ={
            "KIS_LIVE_APP_KEY": "app-key",
            "KIS_LIVE_APP_SECRET": "app-secret",
            "KIS_ISA_ACCOUNT": "1234567801",
            "KIS_US_REGULAR_ACCOUNT": "8765432101",
        },
    )
    adapter = KisLiveReadOnlyBrokerAdapter(client)
    intent = _manual_intent()
    market_price = MarketPrice(
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal("71000"),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(KisLiveOrderBlockedError):
        adapter.submit_order(intent, market_price)

    assert transport.calls == []


def test_paper_role_rejected_for_get_cash() -> None:
    adapter = _make_adapter()

    with pytest.raises(KisAccountRoleError, match="PAPER"):
        adapter.get_cash(Currency.KRW, AccountRole.PAPER)


def test_cash_buffer_not_in_execution_account_roles() -> None:
    assert AccountRole.CASH_BUFFER not in KisLiveReadOnlyBrokerAdapter._EXECUTION_ACCOUNT_ROLES
