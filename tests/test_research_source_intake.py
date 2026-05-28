from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import DuplicateDateIdError, SQLiteDateIdSourceStore
from research_source_intake import (
    IntakeError,
    export_date_md,
    main,
    parse_jsonl_records,
    render_date_md,
    run_export_only,
    run_normal,
    run_validate_only,
)

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"


def _record_dict(
    *,
    date_id: str = "260528-1",
    fact_type: str = "manual",
    summary: str = "Synthetic manual research source for Foundation 8B test.",
) -> dict[str, object]:
    return {
        "date_id": date_id,
        "fact_type": fact_type,
        "source_name": "operator-test",
        "source_timestamp": KST_TS,
        "created_at": KST_CREATED,
        "summary": summary,
        "payload": {"note": "synthetic", "score": 1},
        "symbol": "SYNTH-KR-0001",
        "market": "KR",
        "source_url": "https://example.invalid/autostock/synthetic",
    }


def _write_jsonl(path: Path, *records: dict[str, object]) -> None:
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_validate_only_accepts_valid_jsonl_without_writing_files(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    store = tmp_path / "date_id_sources.sqlite3"
    date_md = tmp_path / "Date.md"
    _write_jsonl(source, _record_dict())

    payload = run_validate_only(source)

    assert payload["status"] == "ok"
    assert payload["mode"] == "validate-only"
    assert payload["records_valid"] == 1
    assert not store.exists()
    assert not date_md.exists()


def test_normal_mode_saves_records_and_exports_date_md(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    store = tmp_path / "date_id_sources.sqlite3"
    date_md = tmp_path / "Date.md"
    _write_jsonl(source, _record_dict())

    payload = run_normal(
        source_jsonl=source,
        store_path=store,
        date_md_out=date_md,
        force_date_md=False,
    )

    assert payload["status"] == "ok"
    assert payload["records_saved"] == 1
    assert store.is_file()
    assert date_md.is_file()

    sqlite_store = SQLiteDateIdSourceStore(store)
    try:
        records = sqlite_store.list_records()
    finally:
        sqlite_store.close()
    assert len(records) == 1
    assert records[0].date_id.value == "260528-1"


def test_date_md_includes_required_fields_and_payload_hash(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    store = tmp_path / "date_id_sources.sqlite3"
    date_md = tmp_path / "Date.md"
    record = _record_dict()
    _write_jsonl(source, record)

    run_normal(
        source_jsonl=source,
        store_path=store,
        date_md_out=date_md,
        force_date_md=False,
    )

    text = date_md.read_text(encoding="utf-8")
    assert "260528-1" in text
    assert "**fact_type:** manual" in text
    assert "**source_name:** operator-test" in text
    assert "**source_timestamp:**" in text
    assert record["summary"] in text
    assert "**symbol:** SYNTH-KR-0001" in text
    assert "**market:** KR" in text
    assert "**source_url:** https://example.invalid/autostock/synthetic" in text
    assert "**payload_hash:**" in text


def test_date_md_does_not_include_raw_payload_json(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    _write_jsonl(source, _record_dict())
    records, _ = parse_jsonl_records(source)
    markdown = render_date_md(records)

    assert '"note"' not in markdown
    assert '"score"' not in markdown
    assert "payload_hash" in markdown


def test_malformed_jsonl_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "bad.jsonl"
    source.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(IntakeError, match="invalid JSON") as exc_info:
        parse_jsonl_records(source)
    assert exc_info.value.stage == "parse"


def test_extra_field_fails_closed(tmp_path: Path) -> None:
    payload = _record_dict()
    payload["unexpected"] = "value"
    source = tmp_path / "extra.jsonl"
    _write_jsonl(source, payload)

    with pytest.raises(IntakeError, match="Extra inputs are not permitted|extra_forbidden"):
        parse_jsonl_records(source)


def test_timezone_naive_datetime_fails_closed(tmp_path: Path) -> None:
    payload = _record_dict()
    payload["source_timestamp"] = "2026-05-28T09:00:00"
    source = tmp_path / "naive.jsonl"
    _write_jsonl(source, payload)

    with pytest.raises(IntakeError, match="timezone-aware"):
        parse_jsonl_records(source)


def test_duplicate_date_id_in_input_fails_before_store_write(tmp_path: Path) -> None:
    source = tmp_path / "dup.jsonl"
    _write_jsonl(source, _record_dict(date_id="260528-1"), _record_dict(date_id="260528-1"))
    store = tmp_path / "date_id_sources.sqlite3"

    with pytest.raises(IntakeError, match="duplicate date_id in input"):
        run_normal(
            source_jsonl=source,
            store_path=store,
            date_md_out=tmp_path / "Date.md",
            force_date_md=False,
        )
    assert not store.exists()


def test_duplicate_date_id_against_existing_store_fails_closed(tmp_path: Path) -> None:
    store = tmp_path / "date_id_sources.sqlite3"
    source = tmp_path / "seed.jsonl"
    _write_jsonl(source, _record_dict())
    records, _ = parse_jsonl_records(source)
    record = records[0]

    sqlite_store = SQLiteDateIdSourceStore(store)
    with sqlite_store.transaction():
        sqlite_store.save_record(record)
    sqlite_store.close()

    intake_source = tmp_path / "research_sources.jsonl"
    _write_jsonl(intake_source, _record_dict(date_id="260528-1"))

    with pytest.raises(IntakeError, match="date_id already exists"):
        run_normal(
            source_jsonl=intake_source,
            store_path=store,
            date_md_out=tmp_path / "Date.md",
            force_date_md=False,
        )


def test_export_only_exports_existing_store_without_source_jsonl(tmp_path: Path) -> None:
    store = tmp_path / "date_id_sources.sqlite3"
    date_md = tmp_path / "Date.exported.md"
    seed = tmp_path / "seed.jsonl"
    _write_jsonl(seed, _record_dict())
    records, _ = parse_jsonl_records(seed)
    record = records[0]

    sqlite_store = SQLiteDateIdSourceStore(store)
    with sqlite_store.transaction():
        sqlite_store.save_record(record)
    sqlite_store.close()

    payload = run_export_only(store_path=store, date_md_out=date_md, force_date_md=False)

    assert payload["status"] == "ok"
    assert payload["mode"] == "export-only"
    assert date_md.is_file()
    assert "260528-1" in date_md.read_text(encoding="utf-8")


def test_existing_date_md_fails_without_force(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    store = tmp_path / "date_id_sources.sqlite3"
    date_md = tmp_path / "Date.md"
    date_md.write_text("# existing\n", encoding="utf-8")
    _write_jsonl(source, _record_dict())

    with pytest.raises(IntakeError, match="already exists"):
        run_normal(
            source_jsonl=source,
            store_path=store,
            date_md_out=date_md,
            force_date_md=False,
        )


def test_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "research_sources.jsonl"
    _write_jsonl(source, _record_dict())

    exit_code = main(["--source-jsonl", str(source), "--validate-only", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert payload["mode"] == "validate-only"
    assert "payload" not in captured.out
    assert "app_key" not in captured.out.lower()


def test_validate_only_and_export_only_together_fail(tmp_path: Path) -> None:
    exit_code = main(
        [
            "--validate-only",
            "--export-only",
            "--source-jsonl",
            str(tmp_path / "x.jsonl"),
            "--store",
            str(tmp_path / "store.sqlite3"),
            "--date-md-out",
            str(tmp_path / "Date.md"),
        ]
    )
    assert exit_code == 1


def test_force_date_md_with_validate_only_writes_no_files(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    date_md = tmp_path / "Date.md"
    date_md.write_text("# existing\n", encoding="utf-8")
    _write_jsonl(source, _record_dict())

    exit_code = main(
        [
            "--source-jsonl",
            str(source),
            "--validate-only",
            "--force-date-md",
        ]
    )

    assert exit_code == 0
    assert date_md.read_text(encoding="utf-8") == "# existing\n"


def test_acceptance_check_blocks_tracked_runtime_research_paths() -> None:
    script = (REPO_ROOT / "ops" / "acceptance_check.sh").read_text(encoding="utf-8")
    assert "^runtime/research/" in script
    assert "runtime generated artifacts: none" in script


def test_cli_validate_only_subprocess(tmp_path: Path) -> None:
    source = tmp_path / "research_sources.jsonl"
    _write_jsonl(source, _record_dict())
    result = _run_cli("--source-jsonl", str(source), "--validate-only", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "ok"


def test_duplicate_date_id_error_type_preserved(tmp_path: Path) -> None:
    store = tmp_path / "date_id_sources.sqlite3"
    seed = tmp_path / "seed.jsonl"
    _write_jsonl(seed, _record_dict())
    records, _ = parse_jsonl_records(seed)
    record = records[0]

    sqlite_store = SQLiteDateIdSourceStore(store)
    with sqlite_store.transaction():
        sqlite_store.save_record(record)
    sqlite_store.close()

    sqlite_store = SQLiteDateIdSourceStore(store)
    with pytest.raises(DuplicateDateIdError):
        with sqlite_store.transaction():
            sqlite_store.save_record(record)
    sqlite_store.close()
