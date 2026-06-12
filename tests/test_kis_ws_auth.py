"""RTM-6 — KIS websocket approval_key provider tests (fake transport; no network)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from data.kis_ws_auth import KIS_APPROVAL_PATH, KisWsApprovalProvider, KisWsAuthError


@dataclass
class _FakeResponse:
    status_code: int
    json_body: dict[str, Any] | list[Any] | None


class _RecordingTransport:
    def __init__(self, response: _FakeResponse | None = None, *, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_seconds: float,
    ) -> _FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body) if json_body else None,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _provider(transport: _RecordingTransport) -> KisWsApprovalProvider:
    return KisWsApprovalProvider(
        transport=transport,
        approval_base_url="https://example.invalid",
        app_key="APPKEY123",
        app_secret="APPSECRET456",
        timeout_seconds=5.0,
    )


def test_issues_approval_key_and_sends_correct_request() -> None:
    transport = _RecordingTransport(_FakeResponse(200, {"approval_key": "APV-XYZ"}))
    provider = _provider(transport)
    assert provider.issue_approval_key() == "APV-XYZ"

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"https://example.invalid{KIS_APPROVAL_PATH}"
    assert call["json_body"] == {
        "grant_type": "client_credentials",
        "appkey": "APPKEY123",
        "secretkey": "APPSECRET456",
    }


def test_non_200_status_fails_closed() -> None:
    transport = _RecordingTransport(_FakeResponse(403, {"msg": "denied"}))
    provider = _provider(transport)
    with pytest.raises(KisWsAuthError, match="status 403"):
        provider.issue_approval_key()


def test_missing_approval_key_fails_closed() -> None:
    transport = _RecordingTransport(_FakeResponse(200, {"something_else": "x"}))
    provider = _provider(transport)
    with pytest.raises(KisWsAuthError, match="missing approval_key"):
        provider.issue_approval_key()


def test_non_object_body_fails_closed() -> None:
    transport = _RecordingTransport(_FakeResponse(200, None))
    provider = _provider(transport)
    with pytest.raises(KisWsAuthError, match="not a JSON object"):
        provider.issue_approval_key()


def test_transport_exception_is_sanitized() -> None:
    secret_in_error = "APPSECRET456-leaked"
    transport = _RecordingTransport(raise_exc=RuntimeError(secret_in_error))
    provider = _provider(transport)
    with pytest.raises(KisWsAuthError) as excinfo:
        provider.issue_approval_key()
    assert secret_in_error not in str(excinfo.value)
    # chained cause is suppressed (from None) so the raw error does not surface.
    assert excinfo.value.__cause__ is None


def test_error_never_contains_credentials() -> None:
    transport = _RecordingTransport(_FakeResponse(500, {"approval_key": ""}))
    provider = _provider(transport)
    with pytest.raises(KisWsAuthError) as excinfo:
        provider.issue_approval_key()
    message = str(excinfo.value)
    assert "APPKEY123" not in message
    assert "APPSECRET456" not in message


def test_blank_credentials_rejected_at_construction() -> None:
    transport = _RecordingTransport(_FakeResponse(200, {"approval_key": "x"}))
    with pytest.raises(KisWsAuthError):
        KisWsApprovalProvider(
            transport=transport,
            approval_base_url="https://example.invalid",
            app_key="   ",
            app_secret="APPSECRET456",
            timeout_seconds=5.0,
        )


def test_non_positive_timeout_rejected() -> None:
    transport = _RecordingTransport(_FakeResponse(200, {"approval_key": "x"}))
    with pytest.raises(KisWsAuthError):
        KisWsApprovalProvider(
            transport=transport,
            approval_base_url="https://example.invalid",
            app_key="APPKEY123",
            app_secret="APPSECRET456",
            timeout_seconds=0.0,
        )
