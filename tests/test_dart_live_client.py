from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
sys.path.insert(0, str(REPO_ROOT / "src"))

from data import SQLiteDateIdSourceStore
from data.dart_live_client import (
    DartLiveFetchError,
    build_live_snapshot_payload,
    build_sanitized_request_metadata,
    fetch_live_dart_snapshot,
    parse_opendart_list_response,
    redact_secrets,
)
from data.dart_source_fetcher import DartDisclosureSnapshotReplayFetcher
from data.fred_http_client import snapshot_filename_for_payload
from data.research_source_fetcher import write_date_id_source_records_jsonl
from domain import FactType

KST = timezone(timedelta(hours=9))
FETCHED_AT = datetime(2026, 5, 30, 13, 0, 0, tzinfo=KST)
AS_OF = FETCHED_AT
SECRET = "SECRET_DART_KEY_TEST"
SYMBOL = "SYNTH-KR-0001"
CORP_CODE = "00126380"
BGN_DE = "20260530"


def _success_opendart_body() -> dict[str, object]:
    return {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 100,
        "total_count": 2,
        "total_page": 1,
        "list": [
            {
                "corp_code": CORP_CODE,
                "corp_name": "Synthetic Corp",
                "stock_code": "000000",
                "corp_cls": "Y",
                "report_nm": "Synthetic DART report 1",
                "rcept_no": "202605300001",
                "flr_nm": "Synthetic Corp",
                "rcept_dt": "20260530",
                "rm": "",
            },
            {
                "corp_code": CORP_CODE,
                "corp_name": "Synthetic Corp",
                "stock_code": "000000",
                "corp_cls": "Y",
                "report_nm": "Synthetic DART report 2",
                "rcept_no": "202605300002",
                "flr_nm": "Synthetic Corp",
                "rcept_dt": "20260530",
                "rm": "",
            },
        ],
    }


def _fake_transport(
    body: dict[str, object] | None = None,
    *,
    raises: Exception | None = None,
) -> object:
    payload = _success_opendart_body() if body is None else body

    def transport(_params: dict[str, str]) -> dict[str, object]:
        if raises is not None:
            raise raises
        return payload

    return transport


def _fetch_snapshot(
    tmp_path: Path,
    *,
    transport: object,
    fetched_at: datetime = FETCHED_AT,
) -> Path:
    return fetch_live_dart_snapshot(
        symbol=SYMBOL,
        corp_code=CORP_CODE,
        api_key=SECRET,
        api_key_env="DART_API_KEY",
        snapshot_dir=tmp_path,
        fetched_at=fetched_at,
        bgn_de=BGN_DE,
        end_de=None,
        page_count=100,
        transport=transport,  # type: ignore[arg-type]
    )


def _run_intake_validate(jsonl_path: Path) -> subprocess.CompletedProcess[str]:
    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(INTAKE_SCRIPT),
            "--source-jsonl",
            str(jsonl_path),
            "--validate-only",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_success_fake_transport_writes_snapshot_with_sanitized_metadata(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, transport=_fake_transport())
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert payload["source_key"] == "dart"
    assert payload["external_service"] == "opendart"
    assert payload["symbol"] == SYMBOL
    assert payload["provider_corp_code"] == CORP_CODE
    assert payload["request"]["api_key_env"] == "DART_API_KEY"
    assert payload["request"]["api_key_present"] is True
    assert payload["request"]["corp_code"] == CORP_CODE
    assert "crtfc_key" not in payload["request"]
    assert len(payload["disclosures"]) == 2

    first = payload["disclosures"][0]
    assert first["title"] == "Synthetic DART report 1"
    assert first["receipt_no"] == "202605300001"
    assert first["source_timestamp"] == "2026-05-30T00:00:00+09:00"
    assert first["source_url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=202605300001"
    assert first["corp_code"] == CORP_CODE
    assert first["filer_name"] == "Synthetic Corp"

    written = snapshot_path.read_text(encoding="utf-8")
    assert SECRET not in written
    assert "crtfc_key=" not in written.lower()


def test_snapshot_filename_uses_canonical_hash_convention(tmp_path: Path) -> None:
    path_one = _fetch_snapshot(tmp_path, transport=_fake_transport())
    payload_one = json.loads(path_one.read_text(encoding="utf-8"))
    expected_name = snapshot_filename_for_payload(payload_one, fetched_at=FETCHED_AT)
    assert path_one.name == expected_name
    assert path_one.name.startswith("raw_")
    assert path_one.name.endswith(".json")

    later = FETCHED_AT + timedelta(seconds=1)
    path_two = _fetch_snapshot(
        tmp_path,
        transport=_fake_transport({"status": "000", "message": "정상", "list": []}),
        fetched_at=later,
    )
    payload_two = json.loads(path_two.read_text(encoding="utf-8"))
    assert path_two.name == snapshot_filename_for_payload(payload_two, fetched_at=later)
    assert path_two.name != path_one.name


def test_snapshot_replays_through_3a_and_validates_8b(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, transport=_fake_transport())
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    try:
        fetcher = DartDisclosureSnapshotReplayFetcher()
        records = fetcher.normalize_snapshot(
            snapshot_path,
            symbol=SYMBOL,
            as_of=AS_OF,
            store=store,
            limit=10,
        )
    finally:
        store.close()

    assert len(records) == 2
    assert all(record.fact_type == FactType.DISCLOSURE for record in records)
    assert all(record.market is None for record in records)
    assert records[0].symbol == SYMBOL

    jsonl_path = tmp_path / "research_sources.jsonl"
    write_date_id_source_records_jsonl(jsonl_path, records, force=True)
    intake = _run_intake_validate(jsonl_path)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["status"] == "ok"


def test_empty_opendart_list_writes_valid_snapshot_with_empty_disclosures(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(
        tmp_path,
        transport=_fake_transport({"status": "000", "message": "정상", "list": []}),
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["disclosures"] == []

    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    try:
        records = DartDisclosureSnapshotReplayFetcher().normalize_snapshot(
            snapshot_path,
            symbol=SYMBOL,
            as_of=AS_OF,
            store=store,
            limit=10,
        )
    finally:
        store.close()
    assert records == []


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"status": "900", "message": "error", "list": []}, "status must be"),
        ({"status": "000", "message": "정상"}, "list is required"),
        (
            {
                "status": "000",
                "list": [{"rcept_no": "1", "rcept_dt": "20260530", "corp_code": CORP_CODE}],
            },
            "report_nm",
        ),
        (
            {
                "status": "000",
                "list": [{"report_nm": "t", "rcept_dt": "20260530", "corp_code": CORP_CODE}],
            },
            "rcept_no",
        ),
        (
            {
                "status": "000",
                "list": [{"report_nm": "t", "rcept_no": "1", "rcept_dt": "20260530", "corp_code": ""}],
            },
            "corp_code",
        ),
        (
            {
                "status": "000",
                "list": [{"report_nm": "t", "rcept_no": "1", "rcept_dt": "bad", "corp_code": CORP_CODE}],
            },
            "YYYYMMDD",
        ),
    ],
)
def test_invalid_opendart_response_fails_without_snapshot(
    tmp_path: Path,
    body: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(DartLiveFetchError, match=match):
        _fetch_snapshot(tmp_path, transport=_fake_transport(body))
    assert list(tmp_path.glob("raw_*.json")) == []


def test_transport_exception_is_sanitized(tmp_path: Path) -> None:
    with pytest.raises(DartLiveFetchError) as exc_info:
        _fetch_snapshot(
            tmp_path,
            transport=_fake_transport(raises=RuntimeError(f"failed crtfc_key={SECRET}")),
        )
    assert SECRET not in exc_info.value.message
    assert list(tmp_path.glob("raw_*.json")) == []


def test_response_containing_api_key_is_rejected(tmp_path: Path) -> None:
    body = _success_opendart_body()
    body["leaked_key"] = SECRET
    with pytest.raises(DartLiveFetchError, match="api_key"):
        _fetch_snapshot(tmp_path, transport=_fake_transport(body))
    assert list(tmp_path.glob("raw_*.json")) == []


def test_snapshot_collision_fails_before_overwrite(tmp_path: Path) -> None:
    transport = _fake_transport()
    first = _fetch_snapshot(tmp_path, transport=transport)
    assert first.is_file()
    with pytest.raises(FileExistsError, match="snapshot already exists"):
        _fetch_snapshot(tmp_path, transport=transport)


def test_transport_none_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(DartLiveFetchError, match="transport is required"):
        fetch_live_dart_snapshot(
            symbol=SYMBOL,
            corp_code=CORP_CODE,
            api_key=SECRET,
            api_key_env="DART_API_KEY",
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
            bgn_de=BGN_DE,
            transport=None,
        )


def test_blank_corp_code_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="corp_code"):
        fetch_live_dart_snapshot(
            symbol=SYMBOL,
            corp_code=" ",
            api_key=SECRET,
            api_key_env="DART_API_KEY",
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
            bgn_de=BGN_DE,
            transport=_fake_transport(),
        )


def test_redact_secrets_removes_known_values_and_query() -> None:
    text = f"error crtfc_key={SECRET} and {SECRET}"
    redacted = redact_secrets(text, secrets=(SECRET,))
    assert SECRET not in redacted
    assert "crtfc_key=[REDACTED]" in redacted


def test_parse_opendart_list_response_maps_two_items() -> None:
    disclosures = parse_opendart_list_response(_success_opendart_body())
    assert len(disclosures) == 2
    assert disclosures[1]["title"] == "Synthetic DART report 2"


def test_dart_live_client_module_has_no_forbidden_tokens() -> None:
    source = (REPO_ROOT / "src" / "data" / "dart_live_client.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "yfinance",
        "kis",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "os.environ",
    )
    for token in forbidden:
        assert token not in source, f"dart_live_client must not reference {token!r}"


def test_dart_live_client_does_not_import_network_libraries() -> None:
    source = REPO_ROOT / "src" / "data" / "dart_live_client.py"
    text = source.read_text(encoding="utf-8")
    assert "import urllib" not in text
    assert "from urllib" not in text


def test_build_live_snapshot_payload_round_trip_metadata() -> None:
    metadata = build_sanitized_request_metadata(
        api_key_env="DART_API_KEY",
        api_key_present=True,
        corp_code=CORP_CODE,
        bgn_de=BGN_DE,
        end_de=None,
        page_count=100,
    )
    payload = build_live_snapshot_payload(
        symbol=SYMBOL,
        provider_corp_code=CORP_CODE,
        fetched_at=FETCHED_AT,
        request_metadata=metadata,
        opendart_response={"status": "000", "message": "정상"},
        disclosures=[],
    )
    assert payload["source_key"] == "dart"
    assert SECRET not in json.dumps(payload)
