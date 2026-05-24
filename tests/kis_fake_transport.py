from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from broker.kis_client import KIS_TOKEN_PATH
from broker.kis_transport import KisHttpResponse


class FakeKisTransport:
    """테스트용 fake KIS transport."""

    def __init__(self, responses: list[KisHttpResponse] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])
        self._default_token = KisHttpResponse(
            status_code=200,
            headers={},
            text=json.dumps({"access_token": "test-token", "expires_in": 86400, "token_type": "Bearer"}),
            json_body={"access_token": "test-token", "expires_in": 86400, "token_type": "Bearer"},
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_seconds: float,
    ) -> KisHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body) if json_body else None,
                "params": dict(params) if params else None,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        if KIS_TOKEN_PATH in url:
            return self._default_token
        if "inquire-balance" in url:
            return KisHttpResponse(
                status_code=200,
                headers={},
                text=json.dumps(
                    {
                        "rt_cd": "0",
                        "output1": [{"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000", "prpr": "71000"}],
                        "output2": [{"ord_psbl_cash": "5000000"}],
                    }
                ),
                json_body={
                    "rt_cd": "0",
                    "output1": [{"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000", "prpr": "71000"}],
                    "output2": [{"ord_psbl_cash": "5000000"}],
                },
            )
        if "inquire-asking-price" in url:
            if "overseas" in url:
                return KisHttpResponse(
                    status_code=200,
                    headers={},
                    text=json.dumps({"rt_cd": "0", "output1": {"bidp": "150.00", "askp": "150.50"}}),
                    json_body={"rt_cd": "0", "output1": {"bidp": "150.00", "askp": "150.50"}},
                )
            return KisHttpResponse(
                status_code=200,
                headers={},
                text=json.dumps({"rt_cd": "0", "output1": {"bidp1": "70900", "askp1": "71100"}}),
                json_body={"rt_cd": "0", "output1": {"bidp1": "70900", "askp1": "71100"}},
            )
        if "inquire-price" in url and "overseas" not in url:
            return KisHttpResponse(
                status_code=200,
                headers={},
                text=json.dumps({"rt_cd": "0", "output": {"stck_prpr": "71000"}}),
                json_body={"rt_cd": "0", "output": {"stck_prpr": "71000"}},
            )
        if "overseas-price" in url and "quotations/price" in url:
            return KisHttpResponse(
                status_code=200,
                headers={},
                text=json.dumps({"rt_cd": "0", "output": {"last": "150.25"}}),
                json_body={"rt_cd": "0", "output": {"last": "150.25"}},
            )
        raise AssertionError(f"Unexpected URL in fake transport: {url}")
