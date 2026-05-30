from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType
from data.fred_adapter import FredMacroAdapter
from data.market_data import macro_data_point_to_source_record


class FredSnapshotReplayClient:
    """FRED snapshot observation replay client. network 호출 없음."""

    def __init__(self, *, snapshot_series_id: str, observation: Mapping[str, Any]) -> None:
        self._snapshot_series_id = normalize_required_string(
            snapshot_series_id,
            field_name="series_id",
        )
        self._observation = observation

    def get_latest_observation(self, series_id: str) -> dict[str, object]:
        normalized_series_id = normalize_required_string(series_id, field_name="series_id")
        if normalized_series_id != self._snapshot_series_id:
            raise ValueError(
                "series_id mismatch: "
                f"requested {normalized_series_id!r}, snapshot has {self._snapshot_series_id!r}"
            )
        return _coerce_observation_for_adapter(self._observation)


class FredSnapshotReplayFetcher:
    """FRED-like local snapshot → DateIdSourceRecord replay fetcher (1A)."""

    source_key = "fred"

    @property
    def fact_types(self) -> tuple[FactType, ...]:
        return (FactType.MACRO,)

    def normalize_snapshot(
        self,
        snapshot_path: Path,
        *,
        series_id: str,
        as_of: datetime,
        date_id: str,
    ) -> list[DateIdSourceRecord]:
        """local FRED snapshot JSON을 MacroDataPoint 경유 DateIdSourceRecord로 변환한다."""
        snapshot = _load_snapshot_object(snapshot_path)
        snapshot_series_id = _require_snapshot_series_id(snapshot)
        requested_series_id = normalize_required_string(series_id, field_name="series_id")
        if snapshot_series_id != requested_series_id:
            raise ValueError(
                "snapshot series_id mismatch: "
                f"snapshot has {snapshot_series_id!r}, requested {requested_series_id!r}"
            )

        observation = snapshot.get("observation")
        if not isinstance(observation, Mapping):
            raise ValueError("snapshot observation must be a JSON object")

        aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")
        adapter = FredMacroAdapter(
            FredSnapshotReplayClient(
                snapshot_series_id=snapshot_series_id,
                observation=observation,
            )
        )
        point = adapter.fetch_latest_observation(requested_series_id, as_of=aware_as_of)
        record = macro_data_point_to_source_record(point, DateId(date_id))
        return [record]


def fetch_live_snapshot(
    *,
    series_id: str,
    api_key: str,
    snapshot_dir: Path,
    fetched_at: datetime,
    api_key_env: str,
    urlopen_fn: Any | None = None,
    force: bool = False,
) -> Path:
    """live-smoke: FRED HTTP → immutable snapshot (urllib는 fred_http_client 전용)."""
    from data.fred_http_client import (
        DEFAULT_TIMEOUT_SECONDS,
        build_live_snapshot_payload,
        build_sanitized_request_metadata,
        fetch_series_observations_body,
        observation_mapping_from_api_body,
        snapshot_filename_for_payload,
        write_live_snapshot_file,
    )

    normalized_series_id = normalize_required_string(series_id, field_name="series_id")
    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    request_metadata = build_sanitized_request_metadata(
        series_id=normalized_series_id,
        api_key_env=api_key_env,
        api_key_present=bool(api_key.strip()),
    )
    body = fetch_series_observations_body(
        normalized_series_id,
        api_key=api_key,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        urlopen_fn=urlopen_fn,
    )
    observation = observation_mapping_from_api_body(body)
    payload = build_live_snapshot_payload(
        series_id=normalized_series_id,
        fetched_at=aware_fetched_at,
        request_metadata=request_metadata,
        observation=observation,
    )
    filename = snapshot_filename_for_payload(payload, fetched_at=aware_fetched_at)
    snapshot_path = snapshot_dir / filename
    if snapshot_path.exists() and not force:
        raise FileExistsError(
            f"snapshot already exists: {snapshot_path} (use --force to overwrite)"
        )
    write_live_snapshot_file(snapshot_path, payload, api_key=api_key)
    return snapshot_path


def _load_snapshot_object(snapshot_path: Path) -> dict[str, Any]:
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be a JSON object")
    return payload


def _require_snapshot_series_id(snapshot: Mapping[str, Any]) -> str:
    series_id = snapshot.get("series_id")
    if series_id is None:
        raise ValueError("snapshot series_id is required")
    return normalize_required_string(series_id, field_name="series_id")


def _coerce_observation_for_adapter(observation: Mapping[str, Any]) -> dict[str, object]:
    """snapshot observation JSON을 FredMacroAdapter가 기대하는 mapping 형태로 변환한다."""
    coerced: dict[str, object] = dict(observation)
    source_timestamp = coerced.get("source_timestamp")
    if source_timestamp is not None and not isinstance(source_timestamp, datetime):
        coerced["source_timestamp"] = parse_timezone_aware_datetime(
            source_timestamp,
            field_name="source_timestamp",
        )
    return coerced
