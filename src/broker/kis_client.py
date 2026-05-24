from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from config.settings import BrokerAccountRoleSettings, KisLiveSettings, KisReadOnlySettings
from domain.enums import AccountRole, AssetClass, Currency, Market
from domain.market import MarketPrice
from domain.money import Money

from broker.kis_models import (
    IsaSupportStatus,
    KisAccessToken,
    KisAccountRef,
    KisAccountRoleError,
    KisBalanceSnapshot,
    KisClientError,
    KisCredentialError,
    KisHttpError,
    KisOrderbookSnapshot,
    KisPositionSnapshot,
    KisReadOnlySmokeResult,
    mask_account_number,
)
from broker.kis_transport import KisHttpResponse, KisHttpTransport

# KIS OpenAPI endpoint paths — 공식 문서 variant 검증은 P3 backlog.
KIS_TOKEN_PATH = "/oauth2/tokenP"
KIS_DOMESTIC_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
KIS_OVERSEAS_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
KIS_DOMESTIC_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
KIS_OVERSEAS_PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
KIS_DOMESTIC_ORDERBOOK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
KIS_OVERSEAS_ORDERBOOK_PATH = "/uapi/overseas-price/v1/quotations/inquire-asking-price"

TR_ID_DOMESTIC_BALANCE = "TTTC8434R"
TR_ID_OVERSEAS_BALANCE = "TTTS3012R"
TR_ID_DOMESTIC_PRICE = "FHKST01010100"
TR_ID_OVERSEAS_PRICE = "HHDFS00000300"
TR_ID_DOMESTIC_ORDERBOOK = "FHKST01010200"
TR_ID_OVERSEAS_ORDERBOOK = "HHDFS76200200"

ISA_SMOKE_SYMBOL_KR = "411060"


class KisReadOnlyClient:
    """KIS live read-only API 클라이언트. 주문 endpoint는 호출하지 않는다."""

    def __init__(
        self,
        *,
        live_settings: KisLiveSettings,
        account_role_settings: BrokerAccountRoleSettings,
        read_only_settings: KisReadOnlySettings,
        transport: KisHttpTransport,
        environ: Mapping[str, str],
    ) -> None:
        self._live_settings = live_settings
        self._account_role_settings = account_role_settings
        self._read_only_settings = read_only_settings
        self._transport = transport
        self._environ = environ
        self._cached_token: KisAccessToken | None = None

    def issue_access_token(self) -> KisAccessToken:
        app_key = self._require_credential(self._live_settings.app_key_env)
        app_secret = self._require_credential(self._live_settings.app_secret_env)

        response = self._transport.request(
            method="POST",
            url=f"{self._live_settings.base_url}{KIS_TOKEN_PATH}",
            headers={"content-type": "application/json"},
            json_body={
                "grant_type": "client_credentials",
                "appkey": app_key,
                "appsecret": app_secret,
            },
            timeout_seconds=self._read_only_settings.timeout_seconds,
        )
        token = _parse_access_token_response(response)
        self._cached_token = token
        return token

    def resolve_account_ref(self, account_role: AccountRole) -> KisAccountRef:
        env_var = resolve_account_env_var(account_role, self._account_role_settings)
        account_number = self._require_credential(env_var)
        return KisAccountRef(
            account_role=account_role,
            account_env_var=env_var,
            account_number_masked=mask_account_number(account_number),
        )

    def get_balance(self, account_role: AccountRole) -> KisBalanceSnapshot:
        account_ref = self.resolve_account_ref(account_role)
        account_number = self._require_credential(account_ref.account_env_var)
        token = self._ensure_token()

        if account_role in {AccountRole.KR_TAX_ADVANTAGED, AccountRole.CASH_BUFFER}:
            response = self._request_domestic_balance(token, account_number)
            currency = Currency.KRW
        elif account_role == AccountRole.US_REGULAR:
            response = self._request_overseas_balance(token, account_number)
            currency = Currency.USD
        else:
            raise KisAccountRoleError(f"Unsupported KIS account role for balance inquiry: {account_role.value}.")

        return KisBalanceSnapshot(
            account_role=account_role,
            currency=currency,
            cash=_parse_balance_cash(response, currency=currency),
            as_of=datetime.now(tz=UTC),
            raw_payload_hash=_hash_payload(response),
        )

    def list_positions(self, account_role: AccountRole) -> tuple[KisPositionSnapshot, ...]:
        account_ref = self.resolve_account_ref(account_role)
        account_number = self._require_credential(account_ref.account_env_var)
        token = self._ensure_token()

        if account_role in {AccountRole.KR_TAX_ADVANTAGED, AccountRole.CASH_BUFFER}:
            response = self._request_domestic_balance(token, account_number)
            return _parse_domestic_positions(response, account_role=account_role)
        if account_role == AccountRole.US_REGULAR:
            response = self._request_overseas_balance(token, account_number)
            return _parse_overseas_positions(response, account_role=account_role)
        raise KisAccountRoleError(f"Unsupported KIS account role for position inquiry: {account_role.value}.")

    def get_current_price(self, symbol: str, market: Market) -> MarketPrice:
        token = self._ensure_token()
        if market == Market.KR:
            response = self._request_domestic_price(token, symbol)
            price, currency = _parse_domestic_price(response)
        elif market == Market.US:
            response = self._request_overseas_price(token, symbol)
            price, currency = _parse_overseas_price(response)
        else:
            raise KisHttpError(f"Unsupported market for price inquiry: {market.value}.")

        return MarketPrice(
            symbol=symbol,
            market=market,
            currency=currency,
            price=price,
            as_of=datetime.now(tz=UTC),
        )

    def get_orderbook(self, symbol: str, market: Market) -> KisOrderbookSnapshot:
        token = self._ensure_token()
        if market == Market.KR:
            response = self._request_domestic_orderbook(token, symbol)
            bid1, ask1 = _parse_domestic_orderbook(response)
        elif market == Market.US:
            response = self._request_overseas_orderbook(token, symbol)
            bid1, ask1 = _parse_overseas_orderbook(response)
        else:
            raise KisHttpError(f"Unsupported market for orderbook inquiry: {market.value}.")

        return KisOrderbookSnapshot(
            symbol=symbol,
            market=market,
            bid1=bid1,
            ask1=ask1,
            as_of=datetime.now(tz=UTC),
        )

    def run_read_only_smoke_check(
        self,
        *,
        kr_symbol: str = ISA_SMOKE_SYMBOL_KR,
        us_symbol: str = "AAPL",
    ) -> KisReadOnlySmokeResult:
        errors: list[str] = []
        token_ok = False
        balance_ok = False
        quote_ok = False
        orderbook_ok = False
        isa_support_status = IsaSupportStatus.UNKNOWN

        try:
            self.issue_access_token()
            token_ok = True
        except KisClientError as exc:
            errors.append(f"token: {exc}")

        if token_ok:
            if not self._account_role_settings.use_isa_for_kr_and_gold:
                isa_support_status = IsaSupportStatus.SKIPPED
            else:
                try:
                    self.get_balance(AccountRole.KR_TAX_ADVANTAGED)
                    balance_ok = True
                    isa_support_status = IsaSupportStatus.SUPPORTED
                except KisClientError as exc:
                    errors.append(f"isa_balance: {exc}")
                    isa_support_status = IsaSupportStatus.UNSUPPORTED

        if token_ok:
            try:
                self.get_current_price(kr_symbol, Market.KR)
                quote_ok = True
            except KisClientError as exc:
                errors.append(f"quote: {exc}")

        if token_ok:
            try:
                self.get_orderbook(kr_symbol, Market.KR)
                orderbook_ok = True
            except KisClientError as exc:
                errors.append(f"orderbook: {exc}")

        return KisReadOnlySmokeResult(
            token_ok=token_ok,
            balance_ok=balance_ok,
            quote_ok=quote_ok,
            orderbook_ok=orderbook_ok,
            isa_support_status=isa_support_status,
            errors=tuple(errors),
            checked_at=datetime.now(tz=UTC),
        )

    def _ensure_token(self) -> KisAccessToken:
        if self._cached_token is not None:
            return self._cached_token
        return self.issue_access_token()

    def _require_credential(self, env_var: str) -> str:
        value = self._environ.get(env_var, "").strip()
        if not value:
            raise KisCredentialError(f"Missing required KIS credential environment variable: {env_var}.")
        return value

    def _auth_headers(self, token: KisAccessToken, tr_id: str) -> dict[str, str]:
        app_key = self._require_credential(self._live_settings.app_key_env)
        app_secret = self._require_credential(self._live_settings.app_secret_env)
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token.access_token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _request_domestic_balance(self, token: KisAccessToken, account_number: str) -> KisHttpResponse:
        cano, acnt_prdt_cd = _split_account_number(account_number)
        return self._request_authenticated(
            token=token,
            tr_id=TR_ID_DOMESTIC_BALANCE,
            path=KIS_DOMESTIC_BALANCE_PATH,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )

    def _request_overseas_balance(self, token: KisAccessToken, account_number: str) -> KisHttpResponse:
        cano, acnt_prdt_cd = _split_account_number(account_number)
        return self._request_authenticated(
            token=token,
            tr_id=TR_ID_OVERSEAS_BALANCE,
            path=KIS_OVERSEAS_BALANCE_PATH,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )

    def _request_domestic_price(self, token: KisAccessToken, symbol: str) -> KisHttpResponse:
        return self._request_authenticated(
            token=token,
            tr_id=TR_ID_DOMESTIC_PRICE,
            path=KIS_DOMESTIC_PRICE_PATH,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )

    def _request_overseas_price(self, token: KisAccessToken, symbol: str) -> KisHttpResponse:
        return self._request_authenticated(
            token=token,
            tr_id=TR_ID_OVERSEAS_PRICE,
            path=KIS_OVERSEAS_PRICE_PATH,
            params={"AUTH": "", "EXCD": "NAS", "SYMB": symbol},
        )

    def _request_domestic_orderbook(self, token: KisAccessToken, symbol: str) -> KisHttpResponse:
        return self._request_authenticated(
            token=token,
            tr_id=TR_ID_DOMESTIC_ORDERBOOK,
            path=KIS_DOMESTIC_ORDERBOOK_PATH,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )

    def _request_overseas_orderbook(self, token: KisAccessToken, symbol: str) -> KisHttpResponse:
        return self._request_authenticated(
            token=token,
            tr_id=TR_ID_OVERSEAS_ORDERBOOK,
            path=KIS_OVERSEAS_ORDERBOOK_PATH,
            params={"AUTH": "", "EXCD": "NAS", "SYMB": symbol},
        )

    def _request_authenticated(
        self,
        *,
        token: KisAccessToken,
        tr_id: str,
        path: str,
        params: Mapping[str, str],
    ) -> KisHttpResponse:
        response = self._transport.request(
            method="GET",
            url=f"{self._live_settings.base_url}{path}",
            headers=self._auth_headers(token, tr_id),
            params=params,
            timeout_seconds=self._read_only_settings.timeout_seconds,
        )
        _ensure_success_response(response)
        return response


def resolve_account_env_var(account_role: AccountRole, settings: BrokerAccountRoleSettings) -> str:
    if account_role == AccountRole.PAPER:
        raise KisAccountRoleError("AccountRole.PAPER is not valid for KIS live account mapping.")
    if account_role == AccountRole.KR_TAX_ADVANTAGED:
        return settings.kr_tax_advantaged_account_env
    if account_role == AccountRole.US_REGULAR:
        return settings.us_regular_account_env
    if account_role == AccountRole.CASH_BUFFER:
        return settings.cash_buffer_account_env
    raise KisAccountRoleError(f"Unsupported account role for KIS mapping: {account_role.value}.")


def _split_account_number(account_number: str) -> tuple[str, str]:
    normalized = account_number.replace("-", "").strip()
    if len(normalized) < 10:
        raise KisCredentialError("KIS account number must be at least 10 characters for CANO/ACNT_PRDT_CD split.")
    return normalized[:8], normalized[8:10]


def _ensure_success_response(response: KisHttpResponse) -> None:
    if not (200 <= response.status_code < 300):
        raise KisHttpError(
            f"KIS HTTP request failed with status_code={response.status_code}.",
            status_code=response.status_code,
        )
    if response.json_body is None:
        raise KisHttpError("KIS HTTP response body is not valid JSON.")
    if isinstance(response.json_body, dict):
        rt_cd = response.json_body.get("rt_cd")
        if rt_cd is not None and str(rt_cd) != "0":
            msg = response.json_body.get("msg1") or response.json_body.get("msg_cd") or "unknown"
            raise KisHttpError(f"KIS API returned error: rt_cd={rt_cd}, msg={msg}.")


def _parse_access_token_response(response: KisHttpResponse) -> KisAccessToken:
    _ensure_success_response(response)
    assert isinstance(response.json_body, dict)
    access_token = response.json_body.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise KisHttpError("KIS token response missing access_token.")

    expires_in = response.json_body.get("expires_in")
    expires_at = None
    raw_expires_in_seconds = None
    if expires_in is not None:
        try:
            raw_expires_in_seconds = int(expires_in)
            expires_at = datetime.now(tz=UTC) + timedelta(seconds=raw_expires_in_seconds)
        except (TypeError, ValueError):
            raw_expires_in_seconds = None

    token_type = response.json_body.get("token_type")
    return KisAccessToken(
        access_token=access_token,
        token_type=str(token_type) if token_type is not None else None,
        expires_at=expires_at,
        raw_expires_in_seconds=raw_expires_in_seconds,
    )


def _parse_balance_cash(response: KisHttpResponse, *, currency: Currency) -> Money:
    assert isinstance(response.json_body, dict)
    output2 = response.json_body.get("output2")
    if isinstance(output2, list) and output2:
        row = output2[0]
        if isinstance(row, dict):
            for key in ("ord_psbl_cash", "dnca_tot_amt", "nxdy_excc_amt"):
                if key in row and row[key] not in (None, ""):
                    return Money(amount=Decimal(str(row[key])), currency=currency)
    output1 = response.json_body.get("output1")
    if isinstance(output1, list) and output1:
        row = output1[0]
        if isinstance(row, dict):
            for key in ("ord_psbl_cash", "dnca_tot_amt"):
                if key in row and row[key] not in (None, ""):
                    return Money(amount=Decimal(str(row[key])), currency=currency)
    raise KisHttpError("KIS balance response missing cash fields.")


def _parse_domestic_positions(response: KisHttpResponse, *, account_role: AccountRole) -> tuple[KisPositionSnapshot, ...]:
    assert isinstance(response.json_body, dict)
    output1 = response.json_body.get("output1")
    if not isinstance(output1, list):
        return ()

    positions: list[KisPositionSnapshot] = []
    for row in output1:
        if not isinstance(row, dict):
            continue
        symbol = row.get("pdno") or row.get("prdt_name")
        quantity_raw = row.get("hldg_qty") or row.get("ord_qty")
        if not symbol or quantity_raw in (None, "", "0", 0):
            continue
        quantity = Decimal(str(quantity_raw))
        if quantity <= 0:
            continue
        avg_cost = Decimal(str(row.get("pchs_avg_pric") or row.get("avg_prvs") or "0"))
        market_price_raw = row.get("prpr") or row.get("now_pric2")
        market_price = Decimal(str(market_price_raw)) if market_price_raw not in (None, "") else None
        asset_class = AssetClass.GOLD if str(symbol) == ISA_SMOKE_SYMBOL_KR else AssetClass.KR_EQUITY
        positions.append(
            KisPositionSnapshot(
                symbol=str(symbol),
                market=Market.KR,
                account_role=account_role,
                asset_class=asset_class,
                quantity=quantity,
                avg_cost=avg_cost,
                currency=Currency.KRW,
                market_price=market_price,
            )
        )
    return tuple(positions)


def _parse_overseas_positions(response: KisHttpResponse, *, account_role: AccountRole) -> tuple[KisPositionSnapshot, ...]:
    assert isinstance(response.json_body, dict)
    output1 = response.json_body.get("output1")
    if not isinstance(output1, list):
        return ()

    positions: list[KisPositionSnapshot] = []
    for row in output1:
        if not isinstance(row, dict):
            continue
        symbol = row.get("ovrs_pdno") or row.get("pdno")
        quantity_raw = row.get("ovrs_cblc_qty") or row.get("hldg_qty")
        if not symbol or quantity_raw in (None, "", "0", 0):
            continue
        quantity = Decimal(str(quantity_raw))
        if quantity <= 0:
            continue
        avg_cost = Decimal(str(row.get("pchs_avg_pric") or row.get("avg_prvs") or "0"))
        market_price_raw = row.get("now_pric2") or row.get("prpr")
        market_price = Decimal(str(market_price_raw)) if market_price_raw not in (None, "") else None
        positions.append(
            KisPositionSnapshot(
                symbol=str(symbol),
                market=Market.US,
                account_role=account_role,
                asset_class=AssetClass.US_EQUITY,
                quantity=quantity,
                avg_cost=avg_cost,
                currency=Currency.USD,
                market_price=market_price,
            )
        )
    return tuple(positions)


def _parse_domestic_price(response: KisHttpResponse) -> tuple[Decimal, Currency]:
    assert isinstance(response.json_body, dict)
    output = response.json_body.get("output")
    if not isinstance(output, dict):
        raise KisHttpError("KIS domestic price response missing output.")
    price_raw = output.get("stck_prpr") or output.get("last")
    if price_raw in (None, ""):
        raise KisHttpError("KIS domestic price response missing stck_prpr.")
    return Decimal(str(price_raw)), Currency.KRW


def _parse_overseas_price(response: KisHttpResponse) -> tuple[Decimal, Currency]:
    assert isinstance(response.json_body, dict)
    output = response.json_body.get("output")
    if not isinstance(output, dict):
        raise KisHttpError("KIS overseas price response missing output.")
    price_raw = output.get("last") or output.get("stck_prpr")
    if price_raw in (None, ""):
        raise KisHttpError("KIS overseas price response missing last.")
    return Decimal(str(price_raw)), Currency.USD


def _parse_domestic_orderbook(response: KisHttpResponse) -> tuple[Decimal, Decimal]:
    assert isinstance(response.json_body, dict)
    output1 = response.json_body.get("output1")
    if not isinstance(output1, dict):
        raise KisHttpError("KIS domestic orderbook response missing output1.")
    bid1_raw = output1.get("bidp1")
    ask1_raw = output1.get("askp1")
    if bid1_raw in (None, "") or ask1_raw in (None, ""):
        raise KisHttpError("KIS domestic orderbook response missing bidp1/askp1.")
    return Decimal(str(bid1_raw)), Decimal(str(ask1_raw))


def _parse_overseas_orderbook(response: KisHttpResponse) -> tuple[Decimal, Decimal]:
    assert isinstance(response.json_body, dict)
    output1 = response.json_body.get("output1")
    if not isinstance(output1, dict):
        raise KisHttpError("KIS overseas orderbook response missing output1.")
    bid1_raw = output1.get("bidp") or output1.get("bidp1")
    ask1_raw = output1.get("askp") or output1.get("askp1")
    if bid1_raw in (None, "") or ask1_raw in (None, ""):
        raise KisHttpError("KIS overseas orderbook response missing bid/ask.")
    return Decimal(str(bid1_raw)), Decimal(str(ask1_raw))


def _hash_payload(response: KisHttpResponse) -> str | None:
    if response.json_body is None:
        return None
    digest = hashlib.sha256(json.dumps(response.json_body, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


__all__ = [
    "ISA_SMOKE_SYMBOL_KR",
    "KIS_DOMESTIC_BALANCE_PATH",
    "KIS_TOKEN_PATH",
    "KisReadOnlyClient",
    "resolve_account_env_var",
]
