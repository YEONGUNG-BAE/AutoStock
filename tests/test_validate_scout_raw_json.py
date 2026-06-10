from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "validate_scout_raw_json.py"
ACCEPTANCE_CHECK = REPO_ROOT / "ops" / "acceptance_check.sh"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from validate_scout_raw_json import (
    OUTPUT_FILES,
    ValidationError,
    main,
    parse_strict_json_object,
    run_validate_scout_raw_json,
)
from scout.models import ScoutInput, ScoutInputRecord, ScoutSummary

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"
SCOUT_INPUT_CREATED_AT = "2026-05-28T00:00:00+00:00"

MANUAL_SMOKE_RAW: dict[str, object] = {
    "summary_id": "scout-kr-260528-1-smoke-test",
    "created_at": "2026-05-28T11:00:19.469156Z",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic manual research source for Foundation 8D smoke test on SYNTH-KR-0001.",
    "positive_factors": [],
    "negative_factors": [],
    "neutral_factors": [
        {
            "name": "Synthetic Smoke Test Input",
            "summary": "The input data is explicitly identified as a synthetic manual research source.",
            "reasons": [
                {
                    "reason": "The payload note indicates the data is synthetic and intended for a smoke test.",
                    "date_id": "260528-1",
                    "source_name": "operator-smoke",
                    "quote": "synthetic",
                },
                {
                    "reason": "The summary explicitly states this is a synthetic manual research source.",
                    "date_id": "260528-1",
                    "source_name": "operator-smoke",
                    "quote": "Synthetic manual research source for Foundation 8D smoke.",
                },
            ],
        }
    ],
    "metadata": {
        "date_ids": ["260528-1"],
        "foundation": "8D",
        "market_scope": "KR",
        "symbol": "SYNTH-KR-0001",
    },
}


def _sample_record(*, date_id: str = "260528-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-smoke",
        source_timestamp=__import__("datetime").datetime.fromisoformat(KST_TS),
        created_at=__import__("datetime").datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8E test.",
        payload={"note": "synthetic", "score": 1},
        symbol="SYNTH-KR-0001",
        market="KR",
        source_url="https://example.invalid/autostock/synthetic",
    )


def _write_date_md(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    path = tmp_path / "Date.md"
    path.write_text(render_date_md(records), encoding="utf-8")
    return path


def _write_store(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


def _write_scout_input(tmp_path: Path, *, created_at: str = SCOUT_INPUT_CREATED_AT) -> Path:
    record = _sample_record()
    scout_input = ScoutInput(
        created_at=__import__("datetime").datetime.fromisoformat(created_at),
        universe="paper-v0",
        records=(
            ScoutInputRecord(
                date_id=record.date_id,
                fact_type=record.fact_type,
                source_name=record.source_name,
                source_timestamp=record.source_timestamp,
                summary=record.summary,
                symbol=record.symbol,
                market=record.market,
                source_url=record.source_url,
                payload=record.payload,
            ),
        ),
        metadata={"foundation": "8D", "market_scope": "KR", "date_ids": ["260528-1"]},
    )
    path = tmp_path / "scout_input.json"
    path.write_text(json.dumps(scout_input.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _write_raw_json(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "scout_output.kr.raw.json"
    path.write_text(json.dumps(payload if payload is not None else MANUAL_SMOKE_RAW, indent=2) + "\n", encoding="utf-8")
    return path


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


def _validate(
    tmp_path: Path,
    *,
    raw_payload: dict[str, object] | None = None,
    out_dir: Path | None = None,
    store: bool = True,
    force: bool = False,
) -> dict[str, object]:
    record = _sample_record()
    date_md_path = _write_date_md(tmp_path, record)
    scout_input_path = _write_scout_input(tmp_path)
    raw_json_path = _write_raw_json(tmp_path, raw_payload)
    store_path = _write_store(tmp_path, record) if store else None
    target_out = out_dir if out_dir is not None else tmp_path / "out"
    return run_validate_scout_raw_json(
        raw_json_path=raw_json_path,
        scout_input_path=scout_input_path,
        date_md_path=date_md_path,
        out_dir=target_out,
        store_path=store_path,
        force=force,
    )


def test_valid_raw_scout_json_produces_expected_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    payload = _validate(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    for name in OUTPUT_FILES:
        assert (out_dir / name).is_file()


def test_validated_json_round_trips_through_scout_summary(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    validated = json.loads((out_dir / "scout_output.validated.json").read_text(encoding="utf-8"))
    summary = ScoutSummary.model_validate(validated)
    assert summary.summary_id.value == "scout-kr-260528-1-smoke-test"


def test_validation_summary_includes_cited_date_ids_and_factor_counts(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    summary = json.loads((out_dir / "scout_validation_summary.json").read_text(encoding="utf-8"))
    assert summary["cited_date_ids"] == ["260528-1"]
    assert summary["factor_counts"]["neutral"] == 1
    assert summary["created_at_freshness_checked"] is False


def test_validation_txt_includes_pass_lines_without_raw_json_body(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = _write_raw_json(tmp_path)
    _validate(tmp_path, out_dir=out_dir)
    txt = (out_dir / "scout_validation.txt").read_text(encoding="utf-8")
    assert "ScoutSummary schema: PASS" in txt
    assert "ScoutInput membership: PASS" in txt
    assert "Date.md membership: PASS" in txt
    assert "created_at freshness ordering: NOT CHECKED" in txt
    assert "neutral_factors" not in txt
    assert raw_path.read_text(encoding="utf-8") not in txt


def test_invalid_raw_json_parse_fails_closed_without_outputs(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = tmp_path / "bad.raw.json"
    raw_path.write_text('{"broken": }', encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid JSON"):
        run_validate_scout_raw_json(
            raw_json_path=raw_path,
            scout_input_path=_write_scout_input(tmp_path),
            date_md_path=_write_date_md(tmp_path, _sample_record()),
            out_dir=out_dir,
            store_path=None,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_markdown_fenced_json_fails_closed() -> None:
    with pytest.raises(ValidationError, match="markdown fences"):
        parse_strict_json_object('```json\n{"a": 1}\n```')


def test_prose_before_json_fails_closed() -> None:
    with pytest.raises(ValidationError, match="single JSON object"):
        parse_strict_json_object('Here is JSON:\n{"a": 1}')


def test_prose_after_json_fails_closed() -> None:
    with pytest.raises(ValidationError, match="single JSON object"):
        parse_strict_json_object('{"a": 1}\nThanks.')


def test_raw_json_array_fails_closed() -> None:
    with pytest.raises(ValidationError, match="single JSON object"):
        parse_strict_json_object("[1, 2, 3]")


def test_scout_summary_schema_error_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(MANUAL_SMOKE_RAW)
    bad.pop("summary_one_liner")
    with pytest.raises(ValidationError, match="summary_one_liner"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_summary_one_liner_over_200_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(MANUAL_SMOKE_RAW)
    bad["summary_one_liner"] = "x" * 201
    with pytest.raises(ValidationError, match="200"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_cited_date_id_missing_from_scout_input_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = json.loads(json.dumps(MANUAL_SMOKE_RAW))
    bad["neutral_factors"][0]["reasons"][0]["date_id"] = "260528-9"
    with pytest.raises(ValidationError, match="missing from ScoutInput"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    date_md_path = _write_date_md(tmp_path, record)
    scout_input_path = _write_scout_input(tmp_path)
    raw_json_path = _write_raw_json(tmp_path)
    bad = json.loads(json.dumps(MANUAL_SMOKE_RAW))
    bad["neutral_factors"][0]["reasons"] = [bad["neutral_factors"][0]["reasons"][0]]
    bad["neutral_factors"][0]["reasons"][0]["date_id"] = "260528-2"
    raw_json_path.write_text(json.dumps(bad, indent=2), encoding="utf-8")

    extra_record = _sample_record(date_id="260528-2")
    scout_input = ScoutInput.model_validate(json.loads(scout_input_path.read_text(encoding="utf-8")))
    extended = ScoutInput(
        created_at=scout_input.created_at,
        universe=scout_input.universe,
        records=scout_input.records
        + (
            ScoutInputRecord(
                date_id=extra_record.date_id,
                fact_type=extra_record.fact_type,
                source_name=extra_record.source_name,
                source_timestamp=extra_record.source_timestamp,
                summary=extra_record.summary,
                symbol=extra_record.symbol,
                market=extra_record.market,
                source_url=extra_record.source_url,
                payload=extra_record.payload,
            ),
        ),
        metadata=scout_input.metadata,
    )
    scout_input_path.write_text(json.dumps(extended.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="missing from Date.md"):
        run_validate_scout_raw_json(
            raw_json_path=raw_json_path,
            scout_input_path=scout_input_path,
            date_md_path=date_md_path,
            out_dir=out_dir,
            store_path=_write_store(tmp_path, record),
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_bracketed_date_id_in_raw_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = json.loads(json.dumps(MANUAL_SMOKE_RAW))
    bad["neutral_factors"][0]["reasons"][0]["date_id"] = "[260528-1]"
    with pytest.raises(ValidationError):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_universe_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(MANUAL_SMOKE_RAW)
    bad["universe"] = "other-universe"
    with pytest.raises(ValidationError, match="universe mismatch"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_invalid_scout_input_file_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    scout_input_path = tmp_path / "scout_input.json"
    scout_input_path.write_text('{"bad": true}', encoding="utf-8")
    with pytest.raises(ValidationError):
        run_validate_scout_raw_json(
            raw_json_path=_write_raw_json(tmp_path),
            scout_input_path=scout_input_path,
            date_md_path=_write_date_md(tmp_path, _sample_record()),
            out_dir=out_dir,
            store_path=None,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_invalid_date_md_file_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    date_md_path = tmp_path / "Date.md"
    date_md_path.write_text("# empty\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        run_validate_scout_raw_json(
            raw_json_path=_write_raw_json(tmp_path),
            scout_input_path=_write_scout_input(tmp_path),
            date_md_path=date_md_path,
            out_dir=out_dir,
            store_path=None,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_store_date_md_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    extra = _sample_record(date_id="260528-2")
    date_md_path = _write_date_md(tmp_path, record, extra)
    with pytest.raises(ValidationError, match="missing from store"):
        run_validate_scout_raw_json(
            raw_json_path=_write_raw_json(tmp_path),
            scout_input_path=_write_scout_input(tmp_path),
            date_md_path=date_md_path,
            out_dir=out_dir,
            store_path=_write_store(tmp_path, record),
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_existing_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    date_md_path = _write_date_md(tmp_path, record)
    scout_input_path = _write_scout_input(tmp_path)
    raw_json_path = _write_raw_json(tmp_path)
    store_path = _write_store(tmp_path, record)
    now = __import__("datetime").datetime.fromisoformat(SCOUT_INPUT_CREATED_AT)

    run_validate_scout_raw_json(
        raw_json_path=raw_json_path,
        scout_input_path=scout_input_path,
        date_md_path=date_md_path,
        out_dir=out_dir,
        store_path=store_path,
        force=False,
    )
    (out_dir / "scout_output.validated.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="output files already exist") as exc_info:
        run_validate_scout_raw_json(
            raw_json_path=raw_json_path,
            scout_input_path=scout_input_path,
            date_md_path=date_md_path,
            out_dir=out_dir,
            store_path=store_path,
            force=False,
        )
    assert exc_info.value.stage == "write"


def test_force_overwrites_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    date_md_path = _write_date_md(tmp_path, record)
    scout_input_path = _write_scout_input(tmp_path)
    raw_json_path = _write_raw_json(tmp_path)
    store_path = _write_store(tmp_path, record)

    run_validate_scout_raw_json(
        raw_json_path=raw_json_path,
        scout_input_path=scout_input_path,
        date_md_path=date_md_path,
        out_dir=out_dir,
        store_path=store_path,
        force=False,
    )
    (out_dir / "scout_output.validated.json").write_text("{}", encoding="utf-8")

    payload = run_validate_scout_raw_json(
        raw_json_path=raw_json_path,
        scout_input_path=scout_input_path,
        date_md_path=date_md_path,
        out_dir=out_dir,
        store_path=store_path,
        force=True,
    )
    assert payload["status"] == "ok"
    validated = json.loads((out_dir / "scout_output.validated.json").read_text(encoding="utf-8"))
    assert validated["summary_id"] == "scout-kr-260528-1-smoke-test"


def test_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    exit_code = main(
        [
            "--raw-json",
            str(_write_raw_json(tmp_path)),
            "--scout-input",
            str(_write_scout_input(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, record)),
            "--store",
            str(_write_store(tmp_path, record)),
            "--out-dir",
            str(out_dir),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert "error" not in payload


def test_json_and_verbose_keeps_stdout_pure_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    exit_code = main(
        [
            "--raw-json",
            str(_write_raw_json(tmp_path)),
            "--scout-input",
            str(_write_scout_input(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, record)),
            "--out-dir",
            str(out_dir),
            "--json",
            "--verbose",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    json.loads(captured.out.strip())
    assert "verbose:" in captured.err


def test_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0


def test_script_does_not_import_forbidden_modules() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "ollama",
        "openai",
        "httpx",
        "requests",
        "aiohttp",
        "yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
    )
    for token in forbidden:
        assert token not in source


def test_pytest_baseline_synchronized_between_runbook_and_acceptance_check() -> None:
    # F7: pytest 게이트는 하드코딩된 pass-count가 아니라 pytest exit code로 판정한다.
    # 이 가드는 (a) 하드코딩 "N passed" grep 게이트가 acceptance에 재도입되는 것을 막고
    # (b) RUNBOOK/acceptance가 특정 합계 숫자에 묶여 테스트 증감이 거짓 경보가 되지
    # 않게 강제한다. 실제 실패는 exit code(!=0)로만 FAIL 처리된다.
    acceptance_text = ACCEPTANCE_CHECK.read_text(encoding="utf-8")
    runbook_text = RUNBOOK.read_text(encoding="utf-8")

    assert re.search(r'grep -q "\d+ passed"', acceptance_text) is None, (
        "acceptance_check.sh must not gate pytest on a hardcoded pass count"
    )
    assert 'fail "pytest: command failed (exit' in acceptance_text, (
        "acceptance_check.sh must FAIL pytest on non-zero exit code"
    )
    assert re.search(r"\d+ passed", runbook_text) is None, (
        "RUNBOOK must not pin a hardcoded pytest pass count as a gate baseline"
    )


def test_manual_ollama_smoke_shape_raw_json_validates_successfully(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    payload = _validate(tmp_path, out_dir=out_dir)
    assert payload["summary_id"] == "scout-kr-260528-1-smoke-test"
    assert payload["cited_date_ids_count"] == 1


def test_scout_summary_created_at_equal_to_scout_input_is_accepted(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw = dict(MANUAL_SMOKE_RAW)
    raw["created_at"] = SCOUT_INPUT_CREATED_AT
    payload = _validate(tmp_path, raw_payload=raw, out_dir=out_dir)
    assert payload["status"] == "ok"
    summary = json.loads((out_dir / "scout_validation_summary.json").read_text(encoding="utf-8"))
    assert summary["created_at_freshness_checked"] is False
