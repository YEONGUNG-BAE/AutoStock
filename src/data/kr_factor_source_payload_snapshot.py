from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from data.kr_factor_source_adapter import KrFactorSourceAdapterError, load_kr_factor_source_payload
from domain._datetime import require_timezone_aware_datetime

# 3G4-5: immutable raw KR factor source payload snapshot (wrapper 없음). network/env/API key 없음.

StageName = Literal["snapshot"]


class KrFactorSourceSnapshotError(ValueError):
    """KR factor source payload snapshot 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _deterministic_factor_source_json_dumps(payload: Mapping[str, Any]) -> str:
    """factor source payload deterministic JSON (float percentile 필드 포함)."""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_filename_for_factor_source_payload(
    payload: Mapping[str, Any],
    *,
    fetched_at: datetime,
) -> str:
    """immutable factor source payload snapshot 파일명 (raw_factor_source_ prefix)."""
    body = _deterministic_factor_source_json_dumps(payload)
    sha8 = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    compact = aware_fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"raw_factor_source_{compact}_{sha8}.json"


def write_immutable_factor_source_snapshot(
    payload: Mapping[str, Any],
    snapshot_dir: Path,
    *,
    fetched_at: datetime,
) -> Path:
    """fetched source payload → immutable raw snapshot (temp validate → atomic rename)."""
    if not isinstance(payload, Mapping):
        raise KrFactorSourceSnapshotError("snapshot", "payload must be a JSON object")

    require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    nested_payload = dict(payload)

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    temp_path = snapshot_dir / f".tmp_factor_source_{uuid.uuid4().hex}.json"
    final_path: Path | None = None

    try:
        serialized = _deterministic_factor_source_json_dumps(nested_payload) + "\n"
        temp_path.write_text(serialized, encoding="utf-8")
        try:
            load_kr_factor_source_payload(temp_path)
        except KrFactorSourceAdapterError as exc:
            raise KrFactorSourceSnapshotError("snapshot", exc.message) from exc

        filename = snapshot_filename_for_factor_source_payload(nested_payload, fetched_at=fetched_at)
        final_path = snapshot_dir / filename
        if final_path.exists():
            raise FileExistsError(f"factor source snapshot already exists: {final_path}")
        temp_path.rename(final_path)
        temp_path = final_path
        return final_path
    except KrFactorSourceSnapshotError:
        raise
    except FileExistsError:
        raise
    except Exception as exc:
        raise KrFactorSourceSnapshotError("snapshot", str(exc)) from exc
    finally:
        if temp_path.exists() and temp_path != final_path:
            temp_path.unlink()
