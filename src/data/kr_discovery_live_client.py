from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from data.fred_http_client import snapshot_filename_for_payload
from data.kr_discovery_source_adapter import (
    KrDiscoverySourceAdapterError,
    load_kr_discovery_snapshot,
)
from decision.canonical_json import canonical_json_dumps
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string

# 3G3-4A: live-shaped KR discovery snapshot fetcher. 실제 HTTP/network/env 읽기 없음 — transport 주입만.
SOURCE_KEY = "kr_discovery"
SNAPSHOT_VERSION = 1

StageName = Literal["args", "fetch", "snapshot", "complete"]

DiscoveryTransport = Callable[[Mapping[str, str]], Mapping[str, Any]]

_FORBIDDEN_SNAPSHOT_FIELDS = frozenset(
    {
        "corp_code",
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_allocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
        "api_key",
        "crtfc_key",
        "request",
    }
)


class KrDiscoveryLiveFetchError(ValueError):
    """KR discovery live-shaped snapshot fetch 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def build_discovery_request_metadata(
    *,
    market: str,
    universe_hint: str,
    as_of: datetime,
    source_name: str,
) -> dict[str, str]:
    """transport에 전달할 sanitized request metadata (secret/credential 없음)."""
    normalized_market = normalize_required_string(market, field_name="market")
    normalized_universe_hint = normalize_required_string(universe_hint, field_name="universe_hint")
    normalized_source_name = normalize_required_string(source_name, field_name="source_name")
    aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")
    return {
        "market": normalized_market,
        "universe_hint": normalized_universe_hint,
        "as_of": aware_as_of.isoformat(),
        "source_name": normalized_source_name,
    }


def build_live_discovery_snapshot_payload(
    *,
    transport_payload: Mapping[str, Any],
    fetched_at: datetime,
    as_of: datetime,
    market: str,
    universe_hint: str,
    external_service: str,
) -> dict[str, Any]:
    """transport 응답 → 3G3-3 replay 호환 discovery snapshot payload (root explicit assignment only)."""
    records_raw = transport_payload.get("records")
    if not isinstance(records_raw, list) or not records_raw:
        raise KrDiscoveryLiveFetchError("snapshot", "transport payload records must be a non-empty list")

    records: list[Any] = []
    for index, entry in enumerate(records_raw):
        if not isinstance(entry, dict):
            raise KrDiscoveryLiveFetchError(
                "snapshot",
                f"transport payload records[{index}] must be a JSON object",
            )
        records.append(dict(entry))

    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")
    normalized_market = normalize_required_string(market, field_name="market")
    normalized_universe_hint = normalize_required_string(universe_hint, field_name="universe_hint")
    normalized_external_service = normalize_required_string(
        external_service,
        field_name="external_service",
    )

    if normalized_market != "KR":
        raise KrDiscoveryLiveFetchError("snapshot", "market must be 'KR'")

    payload: dict[str, Any] = {
        "source_key": SOURCE_KEY,
        "external_service": normalized_external_service,
        "snapshot_version": SNAPSHOT_VERSION,
        "fetched_at": aware_fetched_at.isoformat(),
        "as_of": aware_as_of.isoformat(),
        "market": normalized_market,
        "universe_hint": normalized_universe_hint,
        "records": records,
    }
    _assert_snapshot_payload_forbidden_fields(payload)
    _assert_snapshot_serialized_safe(payload)
    return payload


def fetch_live_kr_discovery_snapshot(
    *,
    snapshot_dir: Path,
    fetched_at: datetime,
    as_of: datetime,
    market: str,
    universe_hint: str,
    external_service: str,
    transport: DiscoveryTransport | None,
    source_name: str | None = None,
) -> Path:
    """fake/주입 transport → 3G3-3 replay 호환 immutable raw discovery snapshot. candidate pool/universe write 금지."""
    if transport is None:
        raise KrDiscoveryLiveFetchError(
            "args",
            "transport is required for KR discovery snapshot fetch (inject fake transport in tests)",
        )

    effective_source_name = source_name if source_name is not None else external_service
    request_metadata = build_discovery_request_metadata(
        market=market,
        universe_hint=universe_hint,
        as_of=as_of,
        source_name=effective_source_name,
    )

    try:
        transport_payload = transport(request_metadata)
    except Exception as exc:
        raise KrDiscoveryLiveFetchError("fetch", f"discovery transport failed: {exc}") from exc

    if not isinstance(transport_payload, Mapping):
        raise KrDiscoveryLiveFetchError("fetch", "discovery transport must return a JSON object")

    try:
        snapshot_payload = build_live_discovery_snapshot_payload(
            transport_payload=transport_payload,
            fetched_at=fetched_at,
            as_of=as_of,
            market=market,
            universe_hint=universe_hint,
            external_service=external_service,
        )
    except KrDiscoveryLiveFetchError:
        raise
    except Exception as exc:
        raise KrDiscoveryLiveFetchError("snapshot", str(exc)) from exc

    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    return _write_validated_immutable_snapshot(
        snapshot_dir=snapshot_dir,
        payload=snapshot_payload,
        fetched_at=aware_fetched_at,
    )


def _write_validated_immutable_snapshot(
    *,
    snapshot_dir: Path,
    payload: dict[str, Any],
    fetched_at: datetime,
) -> Path:
    """validate-before-commit: temp write → parser round-trip → atomic rename to content-hash filename."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    temp_path = snapshot_dir / f".tmp_discovery_{uuid.uuid4().hex}.json"
    final_path: Path | None = None

    try:
        temp_path.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")
        try:
            load_kr_discovery_snapshot(temp_path)
        except KrDiscoverySourceAdapterError as exc:
            raise KrDiscoveryLiveFetchError("snapshot", exc.message) from exc

        filename = snapshot_filename_for_payload(payload, fetched_at=fetched_at)
        final_path = snapshot_dir / filename
        if final_path.exists():
            raise FileExistsError(f"snapshot already exists: {final_path}")

        temp_path.rename(final_path)
        temp_path = final_path  # rename succeeded; do not delete on success path
        return final_path
    except KrDiscoveryLiveFetchError:
        raise
    except FileExistsError:
        raise
    except Exception as exc:
        raise KrDiscoveryLiveFetchError("snapshot", str(exc)) from exc
    finally:
        if temp_path.exists() and temp_path != final_path:
            temp_path.unlink()


def _assert_snapshot_payload_forbidden_fields(payload: Any, *, path: str = "payload") -> None:
    """snapshot 전체에서 금지 필드명을 재귀적으로 거부한다."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _FORBIDDEN_SNAPSHOT_FIELDS:
                raise KrDiscoveryLiveFetchError(
                    "snapshot",
                    f"snapshot contains forbidden field at {path}.{key}",
                )
            _assert_snapshot_payload_forbidden_fields(value, path=f"{path}.{key}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _assert_snapshot_payload_forbidden_fields(item, path=f"{path}[{index}]")


def _assert_snapshot_serialized_safe(payload: dict[str, Any]) -> None:
    """직렬화 결과에 credential-like 토큰이 없는지 검증한다."""
    serialized = canonical_json_dumps(payload).lower()
    if "api_key=" in serialized:
        raise KrDiscoveryLiveFetchError("snapshot", "snapshot must not contain api_key query parameter")
    if '"api_key"' in serialized or '"crtfc_key"' in serialized:
        raise KrDiscoveryLiveFetchError("snapshot", "snapshot must not contain credential field names")
