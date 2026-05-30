"""Real Intake 3E3 — KR real sample live DART disclosure smoke tests (injected transport only)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
SMOKE_SCRIPT = REPO_ROOT / "ops" / "run_date_md_smoke.py"
SCOUT_SCRIPT = REPO_ROOT / "ops" / "build_scout_manual_packet.py"
OPS_SCRIPT = REPO_ROOT / "ops" / "run_kr_real_dart_smoke.py"

KST = timezone(timedelta(hours=9))
AS_OF = "2026-05-30T13:00:00+09:00"
BGN_DE = "20250101"
FETCHED_AT = datetime(2026, 5, 30, 4, 0, 0, tzinfo=UTC)
DART_SECRET = "SECRET_DART_KEY_TEST"
CORP_SAMSUNG = "00126380"
CORP_HYNIX = "00164779"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType


def _opendart_list_body(*, corp_code: str, stock_code: str, corp_name: str, receipt_no: str) -> dict[str, Any]:
    return {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 100,
        "total_count": 1,
        "total_page": 1,
        "list": [
            {
                "corp_code": corp_code,
                "corp_name": corp_name,
                "stock_code": stock_code,
                "corp_cls": "Y",
                "report_nm": f"Synthetic DART report for {stock_code}",
                "rcept_no": receipt_no,
                "flr_nm": corp_name,
                "rcept_dt": "20260530",
                "rm": "",
            },
        ],
    }


def _kr_real_fake_dart_transport(params: dict[str, str]) -> dict[str, Any]:
    corp_code = params["corp_code"]
    if corp_code == CORP_SAMSUNG:
        return _opendart_list_body(
            corp_code=CORP_SAMSUNG,
            stock_code="005930",
            corp_name="Samsung Electronics",
            receipt_no="202605300001",
        )
    if corp_code == CORP_HYNIX:
        return _opendart_list_body(
            corp_code=CORP_HYNIX,
            stock_code="000660",
            corp_name="SK hynix",
            receipt_no="202605300002",
        )
    raise AssertionError(f"unexpected corp_code in test transport: {corp_code!r}")


def _patch_injected_smoke_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() 경로에 fake transport + 고정 fetched_at + 테스트 API key를 주입한다."""
    import run_kr_real_dart_smoke as smoke_module

    original_run = smoke_module.run_kr_real_dart_smoke

    def inject(**kwargs: object) -> dict[str, object]:
        kwargs = dict(kwargs)
        kwargs["fetched_at"] = FETCHED_AT
        kwargs["transport"] = _kr_real_fake_dart_transport
        kwargs["api_key"] = DART_SECRET
        return original_run(**kwargs)

    monkeypatch.setattr(smoke_module, "run_kr_real_dart_smoke", inject)
    monkeypatch.setenv("DART_API_KEY", DART_SECRET)


def _smoke_argv(
    *,
    snapshot_dir: Path,
    out_jsonl: Path,
    store_path: Path,
    universe_path: Path | None = None,
    mapping_path: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        "--universe",
        str(universe_path or KR_REAL_UNIVERSE),
        "--provider-mapping",
        str(mapping_path or KR_REAL_MAPPING),
        "--store",
        str(store_path),
        "--snapshot-dir",
        str(snapshot_dir),
        "--out-jsonl",
        str(out_jsonl),
        "--as-of",
        AS_OF,
        "--bgn-de",
        BGN_DE,
        "--json",
    ]
    if extra:
        argv.extend(extra)
    return argv


def _run_smoke_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_intake_validate(jsonl_path: Path) -> subprocess.CompletedProcess[str]:
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


def _run_intake_normal(
    *,
    jsonl_path: Path,
    store_path: Path,
    date_md_out: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(INTAKE_SCRIPT),
            "--source-jsonl",
            str(jsonl_path),
            "--store",
            str(store_path),
            "--date-md-out",
            str(date_md_out),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_date_md_smoke_cli(
    *,
    universe_path: Path,
    date_md_path: Path,
    store_path: Path,
    require_symbol_coverage: bool,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    argv = [
        sys.executable,
        str(SMOKE_SCRIPT),
        "--universe",
        str(universe_path),
        "--date-md",
        str(date_md_path),
        "--store",
        str(store_path),
        "--json",
    ]
    if require_symbol_coverage:
        argv.append("--require-symbol-coverage")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_scout_packet_cli(
    *,
    universe_path: Path,
    date_md_path: Path,
    store_path: Path,
    out_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(SCOUT_SCRIPT),
            "--universe",
            str(universe_path),
            "--date-md",
            str(date_md_path),
            "--store",
            str(store_path),
            "--out-dir",
            str(out_dir),
            "--market-scope",
            "KR",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _empty_store(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    store.close()
    return store_path


def _seed_store(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


def _sample_seed_record(*, date_id: str = "260530-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-test",
        source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
        created_at=datetime(2026, 5, 30, 9, 5, 0, tzinfo=KST),
        summary="seed record",
        payload={"note": "seed"},
        symbol="005930",
        market="KR",
    )


def _store_record_count(store_path: Path) -> int:
    conn = sqlite3.connect(store_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM date_id_sources").fetchone()
        return int(row[0])
    finally:
        conn.close()



def test_smoke_loads_kr_real_universe_and_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["universe"] == "kr-real-sample-v0"
    assert payload["provider_mapping"] == "kr-real-provider-mappings-v1"


def test_smoke_fake_dart_writes_two_snapshots_and_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert payload["mode"] == "live-dart-smoke"
    assert payload["symbols_count"] == 2
    assert payload["records_count"] == 2

    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 2
    assert out_jsonl.is_file()

    records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert {record["symbol"] for record in records} == {"005930", "000660"}
    date_ids = [record["date_id"] for record in records]
    assert len(set(date_ids)) == 2
    assert date_ids == ["260530-1", "260530-2"]
    for record in records:
        assert record["fact_type"] == "disclosure"
        assert record["market"] is None
        assert record["source_name"] == "dart"


def test_smoke_date_ids_continue_after_store_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    store_path = _seed_store(tmp_path, _sample_seed_record(date_id="260530-1"))
    count_before = _store_record_count(store_path)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    capsys.readouterr()
    records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert sorted(record["date_id"] for record in records) == ["260530-2", "260530-3"]
    assert _store_record_count(store_path) == count_before


def test_smoke_jsonl_validates_through_8b_validate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["records_valid"] == 2


def test_smoke_8b_normal_and_8c_without_symbol_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    fetch_store_path = _empty_store(tmp_path / "fetch")
    intake_store_path = tmp_path / "intake" / "date_id_sources.sqlite3"
    intake_store_path.parent.mkdir(parents=True, exist_ok=True)
    date_md_path = tmp_path / "Date.md"

    assert (
        main(
            _smoke_argv(
                snapshot_dir=snapshot_dir,
                out_jsonl=out_jsonl,
                store_path=fetch_store_path,
            )
        )
        == 0
    )

    intake = _run_intake_normal(
        jsonl_path=out_jsonl,
        store_path=intake_store_path,
        date_md_out=date_md_path,
    )
    assert intake.returncode == 0, intake.stderr

    smoke = _run_date_md_smoke_cli(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md_path,
        store_path=intake_store_path,
        require_symbol_coverage=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    payload = json.loads(smoke.stdout)
    assert payload["status"] == "ok"


def test_smoke_8c_require_symbol_coverage_fails_for_dart_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    intake_store_path = tmp_path / "intake" / "date_id_sources.sqlite3"
    intake_store_path.parent.mkdir(parents=True, exist_ok=True)
    date_md_path = tmp_path / "Date.md"

    assert (
        main(
            _smoke_argv(
                snapshot_dir=snapshot_dir,
                out_jsonl=out_jsonl,
                store_path=_empty_store(tmp_path / "fetch"),
            )
        )
        == 0
    )
    assert (
        _run_intake_normal(
            jsonl_path=out_jsonl,
            store_path=intake_store_path,
            date_md_out=date_md_path,
        ).returncode
        == 0
    )

    smoke = _run_date_md_smoke_cli(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md_path,
        store_path=intake_store_path,
        require_symbol_coverage=True,
    )
    assert smoke.returncode != 0
    payload = json.loads(smoke.stdout)
    assert payload["status"] == "error"


def test_smoke_scout_packet_includes_dart_disclosures_for_kr_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    intake_store_path = tmp_path / "intake" / "date_id_sources.sqlite3"
    intake_store_path.parent.mkdir(parents=True, exist_ok=True)
    date_md_path = tmp_path / "Date.md"
    scout_out = tmp_path / "scout"

    assert (
        main(
            _smoke_argv(
                snapshot_dir=snapshot_dir,
                out_jsonl=out_jsonl,
                store_path=_empty_store(tmp_path / "fetch"),
            )
        )
        == 0
    )
    assert (
        _run_intake_normal(
            jsonl_path=out_jsonl,
            store_path=intake_store_path,
            date_md_out=date_md_path,
        ).returncode
        == 0
    )

    scout = _run_scout_packet_cli(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md_path,
        store_path=intake_store_path,
        out_dir=scout_out,
    )
    assert scout.returncode == 0, scout.stderr
    scout_input = json.loads((scout_out / "scout_input.json").read_text(encoding="utf-8"))
    disclosures = [
        record
        for record in scout_input["records"]
        if record["fact_type"] == "disclosure"
    ]
    assert len(disclosures) == 2
    assert {record["symbol"] for record in disclosures} == {"005930", "000660"}
    assert all(record["market"] is None for record in disclosures)


def test_smoke_snapshot_collision_fails_before_jsonl_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    capsys.readouterr()
    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    original_bytes = {path: path.read_bytes() for path in snapshot_files}
    out_jsonl.unlink()

    exit_code = main(
        _smoke_argv(
            snapshot_dir=snapshot_dir,
            out_jsonl=out_jsonl,
            store_path=store_path,
            extra=["--force"],
        )
    )
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 1
    assert payload["stage"] == "snapshot"
    for path, content in original_bytes.items():
        assert path.read_bytes() == content
    assert not out_jsonl.exists()


def test_smoke_force_overwrites_out_jsonl_not_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_kr_real_dart_smoke as smoke_module
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    first_snapshots = {path: path.read_bytes() for path in snapshot_dir.glob("raw_*.json")}
    fetched = []
    for path in sorted(snapshot_dir.glob("raw_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched.append(
            smoke_module._FetchedDartSnapshot(
                symbol=payload["symbol"],
                market="KR",
                corp_code=payload["provider_corp_code"],
                snapshot_path=path,
            )
        )
    out_jsonl.write_text("stale-jsonl\n", encoding="utf-8")

    monkeypatch.setattr(
        smoke_module,
        "_fetch_dart_snapshots",
        lambda **kwargs: fetched,
    )
    _patch_injected_smoke_run(monkeypatch)

    assert (
        main(
            _smoke_argv(
                snapshot_dir=snapshot_dir,
                out_jsonl=out_jsonl,
                store_path=store_path,
                extra=["--force"],
            )
        )
        == 0
    )
    records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    for path, content in first_snapshots.items():
        assert path.read_bytes() == content


def test_smoke_missing_dart_mapping_fails_at_mapping(tmp_path: Path) -> None:
    mapping_path = tmp_path / "mapping.toml"
    mapping_path.write_text(
        """
version = 1
name = "missing-dart"
description = "missing dart"

[[mappings]]
symbol = "005930"
market = "KR"
enabled = true
stock_code = "005930"

[[mappings]]
symbol = "000660"
market = "KR"
enabled = true
stock_code = "000660"

[mappings.dart]
corp_code = "00164779"
stock_code = "000660"
corp_name = "SK하이닉스"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result = _run_smoke_cli(
        *_smoke_argv(
            snapshot_dir=tmp_path / "sources" / "dart",
            out_jsonl=tmp_path / "out.jsonl",
            store_path=_empty_store(tmp_path),
            mapping_path=mapping_path,
        )
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "mapping"
    assert "DART" in payload["error"]


def test_smoke_missing_corp_code_fails_at_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_kr_real_dart_smoke as smoke_module
    from data.provider_mapping_registry import DartProviderMapping, ProviderMappingEntry, ProviderMappingRegistry
    from domain.universe import UniverseDefinition, UniverseSymbol

    universe = UniverseDefinition(
        version=1,
        name="test-universe",
        description="test",
        base_market="KR",
        symbols=(
            UniverseSymbol(symbol="005930", market="KR", enabled=True, display_name="Samsung"),
        ),
    )
    registry = ProviderMappingRegistry(
        version=1,
        name="test-registry",
        description=None,
        mappings=(
            ProviderMappingEntry(
                symbol="005930",
                market="KR",
                display_name="Samsung",
                stock_code="005930",
                yfinance=None,
                dart=DartProviderMapping(corp_code="   ", stock_code="005930", corp_name="Samsung"),
                enabled=True,
            ),
        ),
    )

    with pytest.raises(smoke_module.KrRealDartSmokeError) as exc_info:
        smoke_module._validate_dart_mapping_coverage(registry, universe)
    assert exc_info.value.stage == "mapping"
    assert "corp_code" in exc_info.value.message


def test_smoke_missing_env_var_fails_at_args_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_dart_smoke import main

    monkeypatch.delenv("DART_API_KEY", raising=False)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    exit_code = main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 1
    assert list(snapshot_dir.glob("raw_*.json")) == []
    assert not out_jsonl.exists()


def test_smoke_blank_env_var_fails_at_args_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    monkeypatch.setenv("DART_API_KEY", "   ")
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    exit_code = main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"
    assert not out_jsonl.exists()
    assert list(snapshot_dir.glob("raw_*.json")) == []


def test_smoke_blank_api_key_env_fails_at_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    monkeypatch.setenv("DART_API_KEY", DART_SECRET)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    exit_code = main(
        _smoke_argv(
            snapshot_dir=snapshot_dir,
            out_jsonl=out_jsonl,
            store_path=store_path,
            extra=["--api-key-env", "   "],
        )
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_smoke_no_api_key_leak_in_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = _empty_store(tmp_path)

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    captured = capsys.readouterr()
    assert DART_SECRET not in captured.out
    assert DART_SECRET not in captured.err
    for snapshot_path in snapshot_dir.glob("raw_*.json"):
        text = snapshot_path.read_text(encoding="utf-8")
        assert DART_SECRET not in text
        assert "crtfc_key=" not in text.lower()
    assert DART_SECRET not in out_jsonl.read_text(encoding="utf-8")


def test_smoke_does_not_write_store_during_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_kr_real_dart_smoke import main

    _patch_injected_smoke_run(monkeypatch)
    store_path = _empty_store(tmp_path)
    count_before = _store_record_count(store_path)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"

    assert main(_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    assert _store_record_count(store_path) == count_before


def test_ops_script_has_no_forbidden_tokens() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "fred_api_key",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "import yfinance",
        "from yfinance",
    )
    for token in forbidden:
        assert token not in source, f"run_kr_real_dart_smoke.py must not reference {token!r}"


def test_existing_kr_real_price_smoke_tests_still_importable() -> None:
    import test_kr_real_price_smoke  # noqa: F401


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""
