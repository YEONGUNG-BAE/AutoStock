"""RTM-7c.10 — offline rehearsal of the paper-day handoff over synthetic fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ops"))

from rehearse_paper_day_handoff import RehearsalError, main, rehearse  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "paper_day_reports"


def _rehearse(name: str, work_dir: Path):
    return rehearse(
        fixture_dir=FIXTURE_ROOT / name,
        work_dir=work_dir,
        expect_source_kind="kis_live",
        allow_runtime_dir=False,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("pass_startup_like", "PASS"),
        ("no_go_health_not_ready", "NO_GO"),
        ("fail_source_approval_failed", "FAIL"),
        ("needs_review_missing_envelope", "NEEDS_REVIEW"),
        ("fail_sensitive_data_present", "FAIL"),
    ],
)
def test_rehearsal_verdicts(name: str, expected: str, tmp_path: Path) -> None:
    result = _rehearse(name, tmp_path / "work")
    assert result["verdict"] == expected
    report = Path(result["report_path"])
    assert report.is_file()
    assert report.read_text(encoding="utf-8").startswith(
        "# Paper Day Diagnostic Review Report"
    )


def test_missing_envelope_fixture_has_no_envelope(tmp_path: Path) -> None:
    result = _rehearse("needs_review_missing_envelope", tmp_path / "work")
    assert result["envelope_present"] is False
    assert "missing_from_persisted_summary" in result["pass_blockers"]


def test_sensitive_fixture_reports_hard_fail(tmp_path: Path) -> None:
    result = _rehearse("fail_sensitive_data_present", tmp_path / "work")
    assert result["hard_fail"] == ["sensitive_data_present"]


def test_does_not_mutate_fixture_files(tmp_path: Path) -> None:
    fixture = FIXTURE_ROOT / "pass_startup_like"
    before = {p.name: p.read_bytes() for p in fixture.iterdir()}
    _rehearse("pass_startup_like", tmp_path / "work")
    after = {p.name: p.read_bytes() for p in fixture.iterdir()}
    assert before == after


def test_writes_only_under_work_dir(tmp_path: Path) -> None:
    work = tmp_path / "work"
    _rehearse("pass_startup_like", work)
    names = sorted(p.name for p in work.iterdir())
    assert names == ["evidence.jsonl", "review-report.md", "stdout-envelope.json", "summary.json"]


def test_refuses_runtime_dir_without_override(tmp_path: Path) -> None:
    runtime_target = REPO_ROOT / "runtime" / "rehearsal-should-not-exist"
    with pytest.raises(RehearsalError, match="runtime/"):
        rehearse(
            fixture_dir=FIXTURE_ROOT / "pass_startup_like",
            work_dir=runtime_target,
            expect_source_kind="kis_live",
            allow_runtime_dir=False,
        )
    assert not runtime_target.exists()


def test_main_expect_verdict_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--fixture",
            str(FIXTURE_ROOT / "pass_startup_like"),
            "--work-dir",
            str(tmp_path / "work"),
            "--expect-verdict",
            "PASS",
        ]
    )
    assert code == 0
    assert "verdict: PASS" in capsys.readouterr().out


def test_main_expect_verdict_mismatch_exits_one(tmp_path: Path) -> None:
    code = main(
        [
            "--fixture",
            str(FIXTURE_ROOT / "no_go_health_not_ready"),
            "--work-dir",
            str(tmp_path / "work"),
            "--expect-verdict",
            "PASS",
        ]
    )
    assert code == 1


def test_missing_fixture_dir_errors(tmp_path: Path) -> None:
    code = main(
        [
            "--fixture",
            str(FIXTURE_ROOT / "does_not_exist"),
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )
    assert code == 2
