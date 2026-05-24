from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from broker.kis_client import (
    KIS_DOMESTIC_BALANCE_PATH,
    KIS_DOMESTIC_ORDERBOOK_PATH,
    KIS_DOMESTIC_PRICE_PATH,
    KIS_OVERSEAS_ORDERBOOK_PATH,
    KIS_OVERSEAS_PRICE_PATH,
    KIS_TOKEN_PATH,
    KisReadOnlyClient,
    resolve_account_env_var,
)
from broker.kis_models import IsaSupportStatus, KisAccountRoleError, KisCredentialError, KisHttpError
from broker.kis_transport import KisHttpResponse
from config.settings import (
    BrokerAccountRoleSettings,
    BrokerSettings,
    KisLiveSettings,
    KisReadOnlySettings,
)
from domain.enums import AccountRole, Currency, Market
from kis_fake_transport import FakeKisTransport


def _make_client(
    transport: FakeKisTransport,
    environ: dict[str, str] | None = None,
    *,
    account_role_settings: BrokerAccountRoleSettings | None = None,
) -> KisReadOnlyClient:
    env = {
        "KIS_LIVE_APP_KEY": "app-key",
        "KIS_LIVE_APP_SECRET": "app-secret",
        "KIS_ISA_ACCOUNT": "1234567801",
        "KIS_US_REGULAR_ACCOUNT": "8765432101",
        "KIS_CMA_ACCOUNT": "1111222201",
        **(environ or {}),
    }
    return KisReadOnlyClient(
        live_settings=KisLiveSettings(),
        account_role_settings=account_role_settings or BrokerAccountRoleSettings(),
        read_only_settings=KisReadOnlySettings(),
        transport=transport,
        environ=env,
    )


def test_constructor_makes_no_transport_calls() -> None:
    transport = FakeKisTransport()
    _make_client(transport)
    assert transport.calls == []


def test_missing_credential_env_fails_before_transport() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport, environ={"KIS_LIVE_APP_KEY": "", "KIS_LIVE_APP_SECRET": ""})

    with pytest.raises(KisCredentialError, match="KIS_LIVE_APP_KEY"):
        client.issue_access_token()
    assert transport.calls == []


def test_token_request_uses_fake_transport() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    token = client.issue_access_token()

    assert token.access_token == "test-token"
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"
    assert KIS_TOKEN_PATH in transport.calls[0]["url"]
    assert transport.calls[0]["json_body"]["appkey"] == "app-key"


def test_credential_error_does_not_expose_secret_values() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport, environ={"KIS_LIVE_APP_KEY": "", "KIS_LIVE_APP_SECRET": "super-secret-value"})

    with pytest.raises(KisCredentialError) as exc_info:
        client.issue_access_token()

    assert "super-secret-value" not in str(exc_info.value)


def test_balance_request_uses_token_and_account_env() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    balance = client.get_balance(AccountRole.KR_TAX_ADVANTAGED)

    assert balance.cash.amount == Decimal("5000000")
    assert balance.currency == Currency.KRW
    assert any(KIS_DOMESTIC_BALANCE_PATH in call["url"] for call in transport.calls)
    auth_call = next(call for call in transport.calls if "Bearer" in call["headers"].get("authorization", ""))
    assert auth_call["headers"]["tr_id"] == "TTTC8434R"


def test_missing_account_env_fails_before_transport_for_balance() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport, environ={"KIS_ISA_ACCOUNT": ""})

    with pytest.raises(KisCredentialError, match="KIS_ISA_ACCOUNT"):
        client.get_balance(AccountRole.KR_TAX_ADVANTAGED)


def test_current_price_kr_market() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    price = client.get_current_price("005930", Market.KR)

    assert price.price == Decimal("71000")
    assert price.currency == Currency.KRW
    assert any(KIS_DOMESTIC_PRICE_PATH in call["url"] for call in transport.calls)


def test_current_price_us_market() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    price = client.get_current_price("AAPL", Market.US)

    assert price.price == Decimal("150.25")
    assert price.currency == Currency.USD


def test_orderbook_kr_market() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    book = client.get_orderbook("005930", Market.KR)

    assert book.bid1 == Decimal("70900")
    assert book.ask1 == Decimal("71100")
    assert any(KIS_DOMESTIC_ORDERBOOK_PATH in call["url"] for call in transport.calls)


def test_orderbook_us_market() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    book = client.get_orderbook("AAPL", Market.US)

    assert book.bid1 == Decimal("150.00")
    assert book.ask1 == Decimal("150.50")


def test_http_non_2xx_returns_structured_error() -> None:
    transport = FakeKisTransport(
        responses=[
            KisHttpResponse(status_code=200, headers={}, text="{}", json_body={"access_token": "t", "expires_in": 1}),
            KisHttpResponse(status_code=500, headers={}, text="error", json_body=None),
        ]
    )
    client = _make_client(transport)

    with pytest.raises(KisHttpError, match="status_code=500"):
        client.get_balance(AccountRole.KR_TAX_ADVANTAGED)


def test_malformed_json_returns_structured_error() -> None:
    transport = FakeKisTransport(
        responses=[
            KisHttpResponse(status_code=200, headers={}, text="{}", json_body={"access_token": "t", "expires_in": 1}),
            KisHttpResponse(status_code=200, headers={}, text="not-json", json_body=None),
        ]
    )
    client = _make_client(transport)

    with pytest.raises(KisHttpError, match="not valid JSON"):
        client.get_balance(AccountRole.KR_TAX_ADVANTAGED)


def test_api_rt_cd_error_returns_structured_error() -> None:
    transport = FakeKisTransport(
        responses=[
            KisHttpResponse(status_code=200, headers={}, text="{}", json_body={"access_token": "t", "expires_in": 1}),
            KisHttpResponse(
                status_code=200,
                headers={},
                text="{}",
                json_body={"rt_cd": "1", "msg1": "invalid account"},
            ),
        ]
    )
    client = _make_client(transport)

    with pytest.raises(KisHttpError, match="rt_cd=1"):
        client.get_balance(AccountRole.KR_TAX_ADVANTAGED)


def test_resolve_account_env_var_mapping() -> None:
    settings = BrokerAccountRoleSettings()

    assert resolve_account_env_var(AccountRole.KR_TAX_ADVANTAGED, settings) == "KIS_ISA_ACCOUNT"
    assert resolve_account_env_var(AccountRole.US_REGULAR, settings) == "KIS_US_REGULAR_ACCOUNT"
    assert resolve_account_env_var(AccountRole.CASH_BUFFER, settings) == "KIS_CMA_ACCOUNT"


def test_resolve_account_env_var_rejects_paper() -> None:
    with pytest.raises(KisAccountRoleError, match="PAPER"):
        resolve_account_env_var(AccountRole.PAPER, BrokerAccountRoleSettings())


def test_account_ref_masks_account_number() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    ref = client.resolve_account_ref(AccountRole.KR_TAX_ADVANTAGED)

    assert ref.account_env_var == "KIS_ISA_ACCOUNT"
    assert "1234567801" not in ref.account_number_masked


def test_smoke_check_runs_read_only_paths() -> None:
    transport = FakeKisTransport()
    client = _make_client(transport)

    result = client.run_read_only_smoke_check()

    assert result.token_ok is True
    assert result.balance_ok is True
    assert result.quote_ok is True
    assert result.orderbook_ok is True
    assert len(transport.calls) >= 4


def test_smoke_check_skips_isa_balance_when_disabled() -> None:
    transport = FakeKisTransport()
    client = _make_client(
        transport,
        environ={"KIS_ISA_ACCOUNT": ""},
        account_role_settings=BrokerAccountRoleSettings(use_isa_for_kr_and_gold=False),
    )

    result = client.run_read_only_smoke_check()

    assert result.isa_support_status == IsaSupportStatus.SKIPPED
    assert result.balance_ok is False
    assert not any("isa_balance" in error for error in result.errors)
    assert not any("inquire-balance" in call["url"] for call in transport.calls)
    assert result.token_ok is True
    assert result.quote_ok is True
    assert result.orderbook_ok is True
