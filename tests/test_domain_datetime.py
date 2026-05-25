from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime

KST = timezone(timedelta(hours=9))
AWARE = datetime(2026, 1, 15, 10, 0, tzinfo=KST)
NAIVE = datetime(2026, 1, 15, 10, 0)


def test_require_timezone_aware_datetime_accepts_aware_object() -> None:
    result = require_timezone_aware_datetime(AWARE, field_name="created_at")
    assert result == AWARE


def test_require_timezone_aware_datetime_rejects_naive_object() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware_datetime(NAIVE, field_name="created_at")


def test_require_timezone_aware_datetime_rejects_non_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware_datetime("2026-01-15T10:00:00+09:00", field_name="created_at")


def test_parse_timezone_aware_datetime_accepts_aware_object() -> None:
    result = parse_timezone_aware_datetime(AWARE, field_name="created_at")
    assert result == AWARE


def test_parse_timezone_aware_datetime_accepts_offset_string() -> None:
    result = parse_timezone_aware_datetime("2026-01-15T10:00:00+09:00", field_name="created_at")
    assert result == AWARE


def test_parse_timezone_aware_datetime_accepts_z_suffix_string() -> None:
    utc_value = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    result = parse_timezone_aware_datetime("2026-01-15T10:00:00Z", field_name="created_at")
    assert result == utc_value


def test_parse_timezone_aware_datetime_rejects_naive_object() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_timezone_aware_datetime(NAIVE, field_name="created_at")


def test_parse_timezone_aware_datetime_rejects_naive_string() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_timezone_aware_datetime("2026-01-15T10:00:00", field_name="created_at")


def test_parse_timezone_aware_datetime_rejects_invalid_string() -> None:
    with pytest.raises(ValueError):
        parse_timezone_aware_datetime("not-a-datetime", field_name="created_at")
