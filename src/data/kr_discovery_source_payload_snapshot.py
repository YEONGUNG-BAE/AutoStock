from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from decision.canonical_json import canonical_json_dumps
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string

# 3G3-6: immutable raw source-specific discovery payload snapshot. network/env/API key 없음.

SOURCE_KEY = "kr_discovery_source_payload"
SNAPSHOT_VERSION = 1
EXPECTED_SOURCE_FORMAT = "synthetic-provider-v1"

StageName = Literal["source_snapshot"]

_WRAPPER_ROOT_KEYS = frozenset(
    {
        "source_key",
        "snapshot_version",
        "external_service",
        "source_format",
        "fetched_at",
        "payload",
    }
)

_FORBIDDEN_KEY_NAMES = frozenset(
    {
        "endpoint_url",
        "request",
        "api_key",
        "apikey",
        "crtfc_key",
        "dart_api_key",
        "fred_api_key",
        "secret",
        "password",
        "token",
        "access_token",
        "authorization",
        "bearer",
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
    }
)

_NARROW_SERIALIZED_CREDENTIAL_MARKERS = (
    "api_key=",
    "crtfc_key=",
    '"api_key"',
    '"crtfc_key"',
    '"access_token"',
    '"Authorization"',
)


class KrDiscoverySourcePayloadSnapshotError(ValueError):
    """KR discovery source payload snapshot 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def source_payload_snapshot_filename(
    payload: Mapping[str, Any],
    *,
    fetched_at: datetime,
) -> str:
    """immutable source payload snapshot 파일명 (raw_source_ prefix)."""
    body = canonical_json_dumps(dict(payload))
    sha8 = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    compact = aware_fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"raw_source_{compact}_{sha8}.json"


def write_source_payload_snapshot(
    *,
    payload: Mapping[str, Any],
    snapshot_dir: Path,
    fetched_at: datetime,
    external_service: str,
    source_format: str,
) -> Path:
    """fetched source-specific payload → immutable raw source snapshot (temp validate → atomic rename)."""
    if not isinstance(payload, Mapping):
        raise KrDiscoverySourcePayloadSnapshotError("source_snapshot", "payload must be a JSON object")

    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    normalized_external_service = normalize_required_string(
        external_service,
        field_name="external_service",
    )
    normalized_source_format = normalize_required_string(source_format, field_name="source_format")

    nested_payload = dict(payload)
    _assert_no_forbidden_keys(nested_payload, path="payload")

    wrapper: dict[str, Any] = {
        "source_key": SOURCE_KEY,
        "snapshot_version": SNAPSHOT_VERSION,
        "external_service": normalized_external_service,
        "source_format": normalized_source_format,
        "fetched_at": aware_fetched_at.isoformat(),
        "payload": nested_payload,
    }
    _validate_wrapper_schema(wrapper)
    _assert_no_forbidden_keys(wrapper, path="snapshot")
    _assert_narrow_serialized_credential_markers_absent(wrapper)

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    temp_path = snapshot_dir / f".tmp_source_payload_{uuid.uuid4().hex}.json"
    final_path: Path | None = None

    try:
        serialized = canonical_json_dumps(wrapper) + "\n"
        temp_path.write_text(serialized, encoding="utf-8")
        _validate_wrapper_schema_from_file(temp_path)
        filename = source_payload_snapshot_filename(wrapper, fetched_at=aware_fetched_at)
        final_path = snapshot_dir / filename
        if final_path.exists():
            raise FileExistsError(f"source payload snapshot already exists: {final_path}")
        temp_path.rename(final_path)
        temp_path = final_path
        return final_path
    except KrDiscoverySourcePayloadSnapshotError:
        raise
    except FileExistsError:
        raise
    except Exception as exc:
        raise KrDiscoverySourcePayloadSnapshotError("source_snapshot", str(exc)) from exc
    finally:
        if temp_path.exists() and temp_path != final_path:
            temp_path.unlink()


def _validate_wrapper_schema(wrapper: Mapping[str, Any]) -> None:
    unknown_root = set(wrapper.keys()) - _WRAPPER_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            f"unknown source payload snapshot root fields: {joined}",
        )

    source_key = wrapper.get("source_key")
    if source_key != SOURCE_KEY:
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "source_key must be 'kr_discovery_source_payload'",
        )

    snapshot_version = wrapper.get("snapshot_version")
    if snapshot_version != SNAPSHOT_VERSION:
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "snapshot_version must be exactly 1",
        )

    external_service = wrapper.get("external_service")
    if not isinstance(external_service, str) or not external_service.strip():
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "external_service must be a non-blank string",
        )

    source_format = wrapper.get("source_format")
    if not isinstance(source_format, str) or not source_format.strip():
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "source_format must be a non-blank string",
        )

    fetched_at_raw = wrapper.get("fetched_at")
    if not isinstance(fetched_at_raw, str) or not fetched_at_raw.strip():
        raise KrDiscoverySourcePayloadSnapshotError("source_snapshot", "fetched_at must be a string")
    try:
        parsed_fetched_at = datetime.fromisoformat(fetched_at_raw.strip())
    except ValueError as exc:
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "fetched_at must be ISO-8601 datetime",
        ) from exc
    if parsed_fetched_at.tzinfo is None:
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "fetched_at must be timezone-aware",
        )

    nested_payload = wrapper.get("payload")
    if not isinstance(nested_payload, dict):
        raise KrDiscoverySourcePayloadSnapshotError("source_snapshot", "payload must be a JSON object")


def _validate_wrapper_schema_from_file(path: Path) -> None:
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            f"invalid source payload snapshot JSON: {exc.msg}",
        ) from exc
    if not isinstance(raw, dict):
        raise KrDiscoverySourcePayloadSnapshotError(
            "source_snapshot",
            "source payload snapshot root must be a JSON object",
        )
    _validate_wrapper_schema(raw)


def _assert_no_forbidden_keys(value: Any, *, path: str) -> None:
    """dictionary key 이름만 재귀 검사 — 값 substring scan 금지."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise KrDiscoverySourcePayloadSnapshotError(
                    "source_snapshot",
                    f"{path}: object keys must be strings",
                )
            if key.lower() in _FORBIDDEN_KEY_NAMES:
                raise KrDiscoverySourcePayloadSnapshotError(
                    "source_snapshot",
                    f"{path}: forbidden field name present",
                )
            _assert_no_forbidden_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_forbidden_keys(nested, path=f"{path}[{index}]")


def _assert_narrow_serialized_credential_markers_absent(payload: Mapping[str, Any]) -> None:
    serialized = canonical_json_dumps(dict(payload))
    lowered = serialized.lower()
    for marker in _NARROW_SERIALIZED_CREDENTIAL_MARKERS:
        if marker.lower() in lowered:
            raise KrDiscoverySourcePayloadSnapshotError(
                "source_snapshot",
                "serialized source payload snapshot contains credential-like marker",
            )
