from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def canonicalize_payload(value: Any) -> Any:
    """JSON-compatible 값을 deterministic canonical 형태로 정규화한다."""
    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        raise ValueError("float values are not allowed in canonical payloads.")

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal values must be finite.")
        return str(value)

    if isinstance(value, datetime):
        from domain._datetime import require_timezone_aware_datetime

        return require_timezone_aware_datetime(value, field_name="datetime").isoformat()

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("dict keys must be strings.")
            normalized[key] = canonicalize_payload(item)
        return {key: normalized[key] for key in sorted(normalized)}

    if isinstance(value, (list, tuple)):
        return [canonicalize_payload(item) for item in value]

    if isinstance(value, set):
        raise ValueError("set values are not allowed in canonical payloads.")

    raise ValueError(f"unsupported canonical payload type: {type(value)!r}")


def canonical_json_dumps(value: Any) -> str:
    """canonical payload를 deterministic JSON 문자열로 직렬화한다."""
    normalized = canonicalize_payload(value)
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)


def payload_sha256(value: Any) -> str:
    """canonical JSON 기준 sha256 hex digest를 반환한다."""
    digest = hashlib.sha256(canonical_json_dumps(value).encode("utf-8")).hexdigest()
    return digest
