from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from data.fred_http_client import (
    assert_snapshot_payload_safe,
    snapshot_filename_for_payload,
    write_live_snapshot_file,
)
from decision.canonical_json import canonical_json_dumps
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string

# 3B1: OpenDART live-shaped snapshot builder. 실제 HTTP/urllib/env 읽기 없음 — transport 주입만.
SOURCE_KEY = "dart"
EXTERNAL_SERVICE = "opendart"
DEFAULT_API_KEY_ENV = "DART_API_KEY"
DART_DISCLOSURE_URL_TEMPLATE = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"
OPENDART_SUCCESS_STATUS = "000"
KST = timezone(timedelta(hours=9))

_API_KEY_QUERY_PATTERN = re.compile(r"crtfc_key=[^&\s\"']+", re.IGNORECASE)

OpenDartTransport = Callable[[Mapping[str, str]], Mapping[str, Any]]


class DartLiveFetchError(RuntimeError):
    """OpenDART live-shaped fetch 실패. message·snapshot에 api_key 값이 포함되면 안 된다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def redact_secrets(text: str, *, secrets: Sequence[str] = ()) -> str:
    """로그/에러/snapshot 검증용 문자열에서 알려진 secret 및 crtfc_key query를 제거한다."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return _API_KEY_QUERY_PATTERN.sub("crtfc_key=[REDACTED]", redacted)


def build_sanitized_request_metadata(
    *,
    api_key_env: str,
    api_key_present: bool,
    corp_code: str,
    bgn_de: str,
    end_de: str | None,
    page_count: int,
) -> dict[str, Any]:
    """snapshot request metadata. api_key 값·full URL·query string 저장 금지."""
    metadata: dict[str, Any] = {
        "api_key_env": normalize_required_string(api_key_env, field_name="api_key_env"),
        "api_key_present": api_key_present,
        "corp_code": normalize_required_string(corp_code, field_name="corp_code"),
        "bgn_de": normalize_required_string(bgn_de, field_name="bgn_de"),
        "page_count": page_count,
    }
    if end_de is not None:
        metadata["end_de"] = normalize_required_string(end_de, field_name="end_de")
    else:
        metadata["end_de"] = None
    return metadata


def fetch_live_dart_snapshot(
    *,
    symbol: str,
    corp_code: str,
    api_key: str,
    api_key_env: str,
    snapshot_dir: Path,
    fetched_at: datetime,
    bgn_de: str,
    end_de: str | None = None,
    page_count: int = 100,
    transport: OpenDartTransport | None,
) -> Path:
    """fake/주입 transport로 OpenDART list 응답 → 3A replay 호환 immutable snapshot. store/JSONL write 금지."""
    if transport is None:
        raise DartLiveFetchError(
            "transport is required for DART live snapshot fetch "
            "(inject dart_http_client via ops --live-smoke --source dart)"
        )

    normalized_symbol = normalize_required_string(symbol, field_name="symbol")
    normalized_corp_code = normalize_required_string(corp_code, field_name="corp_code")
    if not normalized_corp_code:
        raise DartLiveFetchError("corp_code must be non-blank")
    normalized_api_key = normalize_required_string(api_key, field_name="api_key")
    normalized_api_key_env = normalize_required_string(api_key_env, field_name="api_key_env")
    normalized_bgn_de = normalize_required_string(bgn_de, field_name="bgn_de")
    normalized_end_de = (
        normalize_required_string(end_de, field_name="end_de") if end_de is not None else None
    )
    if page_count <= 0:
        raise DartLiveFetchError("page_count must be greater than 0")
    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")

    request_params = _build_transport_request_params(
        corp_code=normalized_corp_code,
        api_key=normalized_api_key,
        bgn_de=normalized_bgn_de,
        end_de=normalized_end_de,
        page_count=page_count,
    )

    try:
        opendart_body = transport(request_params)
    except Exception as exc:
        safe_message = redact_secrets(str(exc), secrets=(normalized_api_key,))
        raise DartLiveFetchError(f"OpenDART transport failed: {safe_message}") from None

    _reject_response_containing_secrets(opendart_body, secrets=(normalized_api_key,))

    disclosures = parse_opendart_list_response(opendart_body)
    request_metadata = build_sanitized_request_metadata(
        api_key_env=normalized_api_key_env,
        api_key_present=True,
        corp_code=normalized_corp_code,
        bgn_de=normalized_bgn_de,
        end_de=normalized_end_de,
        page_count=page_count,
    )
    snapshot_payload = build_live_snapshot_payload(
        symbol=normalized_symbol,
        provider_corp_code=normalized_corp_code,
        fetched_at=aware_fetched_at,
        request_metadata=request_metadata,
        opendart_response=_compact_opendart_metadata(opendart_body),
        disclosures=disclosures,
    )

    filename = snapshot_filename_for_payload(snapshot_payload, fetched_at=aware_fetched_at)
    snapshot_path = snapshot_dir / filename
    if snapshot_path.exists():
        raise FileExistsError(f"snapshot already exists: {snapshot_path}")

    assert_snapshot_payload_safe(snapshot_payload, api_key=normalized_api_key)
    write_live_snapshot_file(snapshot_path, snapshot_payload, api_key=normalized_api_key)
    return snapshot_path


def parse_opendart_list_response(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """OpenDART list API body → 3A replay disclosures 배열. 실패 시 snapshot 미기록."""
    if not isinstance(body, Mapping):
        raise DartLiveFetchError("OpenDART response root must be a JSON object")

    status = body.get("status")
    if status is None:
        raise DartLiveFetchError("OpenDART response status is required")
    status_text = str(status).strip()
    if status_text != OPENDART_SUCCESS_STATUS:
        message = body.get("message", "")
        raise DartLiveFetchError(
            f"OpenDART response status must be {OPENDART_SUCCESS_STATUS!r}, got {status_text!r}: {message}"
        )

    raw_list = body.get("list")
    if raw_list is None:
        raise DartLiveFetchError("OpenDART response list is required")
    if not isinstance(raw_list, list):
        raise DartLiveFetchError("OpenDART response list must be a JSON array")

    disclosures: list[dict[str, Any]] = []
    for index, item in enumerate(raw_list):
        if not isinstance(item, Mapping):
            raise DartLiveFetchError(f"OpenDART list[{index}] must be a JSON object")
        disclosures.append(_map_opendart_list_item(item, index=index))

    return disclosures


def build_live_snapshot_payload(
    *,
    symbol: str,
    provider_corp_code: str,
    fetched_at: datetime,
    request_metadata: dict[str, Any],
    opendart_response: dict[str, Any],
    disclosures: list[dict[str, Any]],
) -> dict[str, Any]:
    """live-shaped immutable snapshot dict. api_key·crtfc_key·full query URL 미포함."""
    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    return {
        "source_key": SOURCE_KEY,
        "external_service": EXTERNAL_SERVICE,
        "symbol": normalize_required_string(symbol, field_name="symbol"),
        "provider_corp_code": normalize_required_string(
            provider_corp_code,
            field_name="provider_corp_code",
        ),
        "fetched_at": aware_fetched_at.isoformat(),
        "request": request_metadata,
        "opendart_response": opendart_response,
        "disclosures": disclosures,
    }


def _build_transport_request_params(
    *,
    corp_code: str,
    api_key: str,
    bgn_de: str,
    end_de: str | None,
    page_count: int,
) -> dict[str, str]:
    """transport 전용 params. snapshot/request metadata에는 crtfc_key 값을 넣지 않는다."""
    params: dict[str, str] = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "page_count": str(page_count),
    }
    if end_de is not None:
        params["end_de"] = end_de
    return params


def _compact_opendart_metadata(body: Mapping[str, Any]) -> dict[str, Any]:
    """페이징/상태 메타만 보존. list 본문은 disclosures로만 반영한다."""
    keys = ("status", "message", "page_no", "page_count", "total_count", "total_page")
    compact: dict[str, Any] = {}
    for key in keys:
        if key in body:
            compact[key] = body[key]
    return compact


def _map_opendart_list_item(item: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    report_nm = item.get("report_nm")
    if report_nm is None or not str(report_nm).strip():
        raise DartLiveFetchError(f"OpenDART list[{index}] report_nm is required")

    rcept_no = item.get("rcept_no")
    if rcept_no is None or not str(rcept_no).strip():
        raise DartLiveFetchError(f"OpenDART list[{index}] rcept_no is required")

    item_corp_code = item.get("corp_code")
    if item_corp_code is None or not str(item_corp_code).strip():
        raise DartLiveFetchError(f"OpenDART list[{index}] corp_code is required")

    rcept_dt = item.get("rcept_dt")
    if rcept_dt is None or not str(rcept_dt).strip():
        raise DartLiveFetchError(f"OpenDART list[{index}] rcept_dt is required")

    normalized_title = normalize_required_string(str(report_nm), field_name="report_nm")
    normalized_receipt = normalize_required_string(str(rcept_no), field_name="rcept_no")
    normalized_corp_code = normalize_required_string(str(item_corp_code), field_name="corp_code")
    source_timestamp_iso = _rcept_dt_to_source_timestamp_iso(str(rcept_dt).strip(), index=index)

    corp_name = _optional_normalized_string(item.get("corp_name"), field_name="corp_name")
    stock_code = _optional_normalized_string(item.get("stock_code"), field_name="stock_code")
    corp_cls = _optional_normalized_string(item.get("corp_cls"), field_name="corp_cls")
    flr_nm = _optional_normalized_string(item.get("flr_nm"), field_name="flr_nm")
    remark = _optional_normalized_string(item.get("rm"), field_name="rm") or ""

    disclosure: dict[str, Any] = {
        "title": normalized_title,
        "source_timestamp": source_timestamp_iso,
        "source_url": DART_DISCLOSURE_URL_TEMPLATE.format(receipt_no=normalized_receipt),
        "receipt_no": normalized_receipt,
        "corp_code": normalized_corp_code,
        "report_type": normalized_title,
        "remark": remark,
    }
    if corp_name is not None:
        disclosure["corp_name"] = corp_name
    if stock_code is not None:
        disclosure["stock_code"] = stock_code
    if corp_cls is not None:
        disclosure["corp_cls"] = corp_cls
    if flr_nm is not None:
        disclosure["filer_name"] = flr_nm

    return disclosure


def _rcept_dt_to_source_timestamp_iso(rcept_dt: str, *, index: int) -> str:
    if len(rcept_dt) != 8 or not rcept_dt.isdigit():
        raise DartLiveFetchError(f"OpenDART list[{index}] rcept_dt must be YYYYMMDD")
    year = int(rcept_dt[0:4])
    month = int(rcept_dt[4:6])
    day = int(rcept_dt[6:8])
    try:
        source_dt = datetime(year, month, day, 0, 0, 0, tzinfo=KST)
    except ValueError as exc:
        raise DartLiveFetchError(f"OpenDART list[{index}] rcept_dt is invalid: {rcept_dt}") from exc
    return source_dt.isoformat()


def _optional_normalized_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalize_required_string(text, field_name=field_name)


def _reject_response_containing_secrets(
    body: Mapping[str, Any],
    *,
    secrets: Sequence[str],
) -> None:
    serialized = canonical_json_dumps(dict(body))
    for secret in secrets:
        if secret and secret in serialized:
            raise DartLiveFetchError("OpenDART response must not contain api_key value")
    if "crtfc_key=" in serialized.lower():
        raise DartLiveFetchError("OpenDART response must not contain crtfc_key query parameter")
