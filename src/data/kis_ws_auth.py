from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

# 승인키 발급 경로. REST token(/oauth2/tokenP)과 달리 websocket 전용 approval_key를 낸다.
KIS_APPROVAL_PATH = "/oauth2/Approval"


class KisWsAuthError(Exception):
    """approval_key 발급 실패. 메시지에 appkey/secretkey/approval_key를 절대 담지 않는다."""


class _ApprovalHttpResponse(Protocol):
    """주입된 HTTP transport 응답의 구조적 계약(broker import 회피용)."""

    status_code: int
    json_body: dict[str, Any] | list[Any] | None


class ApprovalHttpTransport(Protocol):
    """approval_key POST에 쓰는 HTTP transport의 구조적 계약.

    broker.kis_transport.KisHttpTransport와 호환되지만 이 모듈은 broker를 import하지
    않는다(transport 모듈 import 경계 유지). 테스트는 fake transport를 주입한다.
    """

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_seconds: float,
    ) -> _ApprovalHttpResponse: ...


class KisWsApprovalProvider:
    """KIS websocket approval_key 발급기.

    주문/체결/잔고 endpoint를 호출하지 않으며, appkey/secretkey는 주입받아 보관하지만
    어떤 로그·에러·반환값에도 노출하지 않는다. approval_key 문자열만 반환한다.
    """

    def __init__(
        self,
        *,
        transport: ApprovalHttpTransport,
        approval_base_url: str,
        app_key: str,
        app_secret: str,
        timeout_seconds: float,
    ) -> None:
        if not app_key or not app_key.strip():
            raise KisWsAuthError("app_key is required for approval issuance.")
        if not app_secret or not app_secret.strip():
            raise KisWsAuthError("app_secret is required for approval issuance.")
        if timeout_seconds <= 0:
            raise KisWsAuthError("timeout_seconds must be greater than 0.")
        self._transport = transport
        self._base_url = approval_base_url.rstrip("/")
        self._app_key = app_key
        self._app_secret = app_secret
        self._timeout_seconds = timeout_seconds

    def issue_approval_key(self) -> str:
        try:
            response = self._transport.request(
                method="POST",
                url=f"{self._base_url}{KIS_APPROVAL_PATH}",
                headers={"content-type": "application/json"},
                json_body={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "secretkey": self._app_secret,
                },
                timeout_seconds=self._timeout_seconds,
            )
        except Exception:  # noqa: BLE001 — sanitize all transport failures
            raise KisWsAuthError("approval request transport error.") from None

        status = getattr(response, "status_code", None)
        if status != 200:
            raise KisWsAuthError(f"approval request failed with status {status}.")

        body = getattr(response, "json_body", None)
        if not isinstance(body, dict):
            raise KisWsAuthError("approval response body is not a JSON object.")
        approval_key = body.get("approval_key")
        if not isinstance(approval_key, str) or not approval_key.strip():
            raise KisWsAuthError("approval response is missing approval_key.")
        return approval_key


__all__ = [
    "KIS_APPROVAL_PATH",
    "ApprovalHttpTransport",
    "KisWsApprovalProvider",
    "KisWsAuthError",
]
