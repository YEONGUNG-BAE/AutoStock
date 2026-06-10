from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPO_ROOT / "ops" / "validate_allocator_raw_json.py"
BUILD_SCRIPT = REPO_ROOT / "ops" / "build_allocator_manual_packet.py"
ACCEPTANCE_CHECK = REPO_ROOT / "ops" / "acceptance_check.sh"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"
EXAMPLE_PORTFOLIO = REPO_ROOT / "docs" / "examples" / "portfolio_state.paper.example.json"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from scout.models import ScoutSummary
from validate_allocator_raw_json import (
    OUTPUT_FILES as VALIDATION_FILES,
    ValidationError,
    main,
    parse_strict_json_object,
    run_validate_allocator_raw_json,
)

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"
NOW = datetime(2026, 5, 28, 0, 0, tzinfo=timezone.utc)

MANUAL_SMOKE_RAW: dict[str, object] = {
    "summary_id": "scout-kr-260528-1-smoke-test",
    "created_at": "2026-05-28T11:00:19.469156Z",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic manual research source for Foundation 8F smoke test on SYNTH-KR-0001.",
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
                }
            ],
        }
    ],
    "metadata": {"date_ids": ["260528-1"], "foundation": "8F", "market_scope": "KR"},
}

VALID_ALLOCATOR_RAW: dict[str, object] = {
    "decision_id": "allocator-260528-1-smoke-test",
    "created_at": "2026-05-28T12:00:00+09:00",
    "schema_name": "allocator_decision.v1",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic allocation decision for Foundation 8F validator smoke.",
    "gold_policy_mode": "normal",
    "signal_summary": {
        "summary": "Risk regime remains balanced.",
        "reasons": [
            {"reason": "Synthetic signal basis.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "cash_manager": {
        "summary": "Maintain 20% cash.",
        "recommended_cash_percent": "20",
        "reasons": [
            {"reason": "Synthetic cash rationale.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "asset_allocator": {
        "summary": "Use balanced KR/US/Gold mix.",
        "target_weights": {"kr": "50", "us": "30", "gold": "20"},
        "reasons": [
            {
                "reason": "Synthetic asset allocation rationale.",
                "date_id": "260528-1",
                "source_name": "operator-smoke",
            }
        ],
    },
    "consistency_checker": {
        "passed": True,
        "summary": "All fields are consistent.",
        "issues": [],
        "reasons": [
            {"reason": "Consistency smoke reason.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "cash_policy": {
        "cash_target_percent": "20",
        "rationale": "Keep liquidity buffer.",
        "reasons": [
            {"reason": "Cash policy smoke reason.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "target_weights": {"kr": "50", "us": "30", "gold": "20"},
    "reasons": [
        {"reason": "Top-level synthetic rationale.", "date_id": "260528-1", "source_name": "operator-smoke"}
    ],
    "metadata": {"foundation": "8F", "note": "manual smoke"},
}


def _sample_record(*, date_id: str = "260528-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-smoke",
        source_timestamp=datetime.fromisoformat(KST_TS),
        created_at=datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8F test.",
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


def _write_allocator_input(
    tmp_path: Path,
    *,
    universe: str = "paper-v0",
    allowed_date_ids: list[str] | None = None,
) -> Path:
    path = tmp_path / "allocator_input.json"
    scout_summary = ScoutSummary.model_validate(
        {**MANUAL_SMOKE_RAW, "universe": universe},
    ).model_dump(mode="json")
    portfolio_state = json.loads(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"))
    payload = {
        "created_at": "2026-05-28T00:00:00+00:00",
        "universe": universe,
        "scout_summary": scout_summary,
        "portfolio_state": portfolio_state,
        "allowed_date_ids": allowed_date_ids if allowed_date_ids is not None else ["260528-1"],
        "allocator_schema_summary": {
            "schema_name": "allocator_decision.v1",
            "summary_one_liner_max_length": 200,
            "target_weights_must_sum_to": "100",
            "gold_policy_modes": ["normal", "exception"],
            "consistency_checker_passed_required": True,
        },
        "constraints": portfolio_state["constraints"],
        "metadata": {
            "foundation": "8F",
            "scout_summary_id": "scout-kr-260528-1-smoke-test",
            "portfolio_snapshot_id": portfolio_state["portfolio_snapshot"]["snapshot_id"],
            "nav_snapshot_id": portfolio_state["nav_snapshot"]["snapshot_id"],
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_raw_json(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "allocator_output.raw.json"
    path.write_text(json.dumps(payload if payload is not None else VALID_ALLOCATOR_RAW, indent=2) + "\n", encoding="utf-8")
    return path


def _validate(
    tmp_path: Path,
    *,
    raw_payload: dict[str, object] | None = None,
    out_dir: Path | None = None,
    allocator_input_path: Path | None = None,
    date_md_path: Path | None = None,
    store_path: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    record = _sample_record()
    target_out = out_dir if out_dir is not None else tmp_path / "out"
    return run_validate_allocator_raw_json(
        raw_json_path=_write_raw_json(tmp_path, raw_payload),
        allocator_input_path=allocator_input_path or _write_allocator_input(tmp_path),
        date_md_path=date_md_path or _write_date_md(tmp_path, record),
        out_dir=target_out,
        store_path=store_path if store_path is not None else _write_store(tmp_path, record),
        now=NOW,
        force=force,
    )


def test_22_valid_allocator_raw_json_produces_expected_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    payload = _validate(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    for name in VALIDATION_FILES:
        assert (out_dir / name).is_file()


def test_23_validated_json_round_trips_through_allocator_decision_schema(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    validated = json.loads((out_dir / "allocator_output.validated.json").read_text(encoding="utf-8"))
    assert validated["decision_id"] == "allocator-260528-1-smoke-test"
    assert validated["target_weights"] == {"kr": "50", "us": "30", "gold": "20"}


def test_24_validation_summary_includes_expected_fields(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    summary = json.loads((out_dir / "allocator_validation_summary.json").read_text(encoding="utf-8"))
    assert summary["cited_date_ids"] == ["260528-1"]
    assert summary["target_weights"] == {"kr": "50", "us": "30", "gold": "20"}
    assert summary["cash_target_percent"] == "20"
    assert summary["gold_policy_mode"] == "normal"
    assert summary["created_at_freshness_checked"] is False


def test_25_validation_txt_includes_pass_lines_without_raw_json_body(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = _write_raw_json(tmp_path)
    _validate(tmp_path, out_dir=out_dir)
    txt = (out_dir / "allocator_validation.txt").read_text(encoding="utf-8")
    assert "AllocatorDecision schema: PASS" in txt
    assert "allocator_input membership: PASS" in txt
    assert "Date.md membership: PASS" in txt
    assert "created_at freshness ordering: NOT CHECKED" in txt
    assert raw_path.read_text(encoding="utf-8") not in txt


def test_26_invalid_raw_json_parse_fails_closed_without_outputs(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = tmp_path / "allocator_output.raw.json"
    raw_path.write_text('{"broken": }', encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid JSON"):
        run_validate_allocator_raw_json(
            raw_json_path=raw_path,
            allocator_input_path=_write_allocator_input(tmp_path),
            date_md_path=_write_date_md(tmp_path, _sample_record()),
            out_dir=out_dir,
            store_path=_write_store(tmp_path, _sample_record()),
            now=NOW,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_27_markdown_fenced_json_fails_closed() -> None:
    with pytest.raises(ValidationError, match="markdown fences"):
        parse_strict_json_object('```json\n{"a": 1}\n```')


def test_28_prose_before_json_fails_closed() -> None:
    with pytest.raises(ValidationError, match="single JSON object"):
        parse_strict_json_object('Here is JSON:\n{"a": 1}')


def test_29_prose_after_json_fails_closed() -> None:
    with pytest.raises(ValidationError, match="single JSON object"):
        parse_strict_json_object('{"a": 1}\nThanks.')


def test_30_raw_json_array_fails_closed() -> None:
    with pytest.raises(ValidationError, match="single JSON object"):
        parse_strict_json_object("[1, 2, 3]")


def test_31_allocator_schema_error_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ALLOCATOR_RAW)
    bad.pop("summary_one_liner")
    with pytest.raises(ValidationError, match="summary_one_liner"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_32_summary_one_liner_over_200_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ALLOCATOR_RAW)
    bad["summary_one_liner"] = "x" * 201
    with pytest.raises(ValidationError, match="200"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_33_universe_mismatch_between_raw_and_allocator_input_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ALLOCATOR_RAW)
    bad["universe"] = "other-universe"
    with pytest.raises(ValidationError, match="universe mismatch"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_34_cited_date_id_missing_from_allocator_input_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    allocator_input_path = _write_allocator_input(tmp_path, allowed_date_ids=["260528-9"])
    with pytest.raises(ValidationError, match="missing from allocator_input.allowed_date_ids"):
        _validate(tmp_path, allocator_input_path=allocator_input_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_35_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    wrong_record = _sample_record(date_id="260528-9")
    with pytest.raises(ValidationError, match="missing from Date.md"):
        _validate(
            tmp_path,
            date_md_path=_write_date_md(tmp_path, wrong_record),
            store_path=_write_store(tmp_path, wrong_record),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_36_bracketed_date_id_in_raw_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = json.loads(json.dumps(VALID_ALLOCATOR_RAW))
    bad["reasons"][0]["date_id"] = "[260528-1]"
    with pytest.raises(ValidationError):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_37_invalid_allocator_input_file_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    allocator_input_path = tmp_path / "allocator_input.json"
    allocator_input_path.write_text('{"bad": true}', encoding="utf-8")
    with pytest.raises(ValidationError):
        run_validate_allocator_raw_json(
            raw_json_path=_write_raw_json(tmp_path),
            allocator_input_path=allocator_input_path,
            date_md_path=_write_date_md(tmp_path, _sample_record()),
            out_dir=out_dir,
            store_path=_write_store(tmp_path, _sample_record()),
            now=NOW,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_38_invalid_date_md_file_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    date_md_path = tmp_path / "Date.md"
    date_md_path.write_text("# empty\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        _validate(tmp_path, date_md_path=date_md_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_39_store_date_md_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    extra = _sample_record(date_id="260528-2")
    with pytest.raises(ValidationError, match="Date.md date_id missing from store"):
        _validate(
            tmp_path,
            date_md_path=_write_date_md(tmp_path, record, extra),
            store_path=_write_store(tmp_path, record),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_40_business_rule_failure_with_store_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = json.loads(json.dumps(VALID_ALLOCATOR_RAW))
    bad["consistency_checker"]["passed"] = False
    with pytest.raises(ValidationError, match="consistency_checker"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in VALIDATION_FILES)


def test_41_existing_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    allocator_input_path = _write_allocator_input(tmp_path)
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    _validate(
        tmp_path,
        allocator_input_path=allocator_input_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
    )
    (out_dir / "allocator_output.validated.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="output files already exist") as exc_info:
        _validate(
            tmp_path,
            allocator_input_path=allocator_input_path,
            date_md_path=date_md_path,
            store_path=store_path,
            out_dir=out_dir,
            force=False,
        )
    assert exc_info.value.stage == "write"


def test_42_force_overwrites_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    allocator_input_path = _write_allocator_input(tmp_path)
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    _validate(
        tmp_path,
        allocator_input_path=allocator_input_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
    )
    (out_dir / "allocator_output.validated.json").write_text("{}", encoding="utf-8")
    payload = _validate(
        tmp_path,
        allocator_input_path=allocator_input_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
        force=True,
    )
    assert payload["status"] == "ok"
    validated = json.loads((out_dir / "allocator_output.validated.json").read_text(encoding="utf-8"))
    assert validated["decision_id"] == "allocator-260528-1-smoke-test"


def test_43_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    exit_code = main(
        [
            "--raw-json",
            str(_write_raw_json(tmp_path)),
            "--allocator-input",
            str(_write_allocator_input(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, record)),
            "--store",
            str(_write_store(tmp_path, record)),
            "--out-dir",
            str(out_dir),
            "--now",
            NOW.isoformat(),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert "error" not in payload


def test_44_json_and_verbose_keeps_stdout_pure_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    exit_code = main(
        [
            "--raw-json",
            str(_write_raw_json(tmp_path)),
            "--allocator-input",
            str(_write_allocator_input(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, record)),
            "--store",
            str(_write_store(tmp_path, record)),
            "--out-dir",
            str(out_dir),
            "--now",
            NOW.isoformat(),
            "--json",
            "--verbose",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    json.loads(captured.out.strip())
    assert "verbose:" in captured.err


def test_45_allocator_ops_scripts_do_not_import_forbidden_modules() -> None:
    source_build = BUILD_SCRIPT.read_text(encoding="utf-8").lower()
    source_validate = VALIDATE_SCRIPT.read_text(encoding="utf-8").lower()
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
        assert token not in source_build
        assert token not in source_validate


def test_46_pytest_baseline_synchronized_between_runbook_and_acceptance_check() -> None:
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


def test_47_manual_smoke_shape_raw_json_validates_successfully(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    payload = _validate(tmp_path, out_dir=out_dir)
    assert payload["decision_id"] == "allocator-260528-1-smoke-test"
    assert payload["cited_date_ids_count"] == 1


def test_48_store_argument_is_required_at_cli_level(tmp_path: Path) -> None:
    """--store 누락 시 argparse가 SystemExit으로 거부해야 한다 (P1 회귀 가드).

    이전 구현은 --store optional이라 누락 시 AllocatorDecisionValidator가 통째 스킵되어
    target_weights sum / gold band / cash band / consistency_checker.passed 등
    핵심 business rule이 검증되지 않은 채 validation.txt가 PASS로 출력되는 위험이 있었다.
    """
    record = _sample_record()
    raw = _write_raw_json(tmp_path)
    alloc_input = _write_allocator_input(tmp_path)
    date_md = _write_date_md(tmp_path, record)
    out_dir = tmp_path / "out"
    with pytest.raises(SystemExit) as exc_info:
        main([
            "--raw-json", str(raw),
            "--allocator-input", str(alloc_input),
            "--date-md", str(date_md),
            "--out-dir", str(out_dir),
        ])
    assert exc_info.value.code != 0
    assert not out_dir.exists() or not any(out_dir.iterdir())
