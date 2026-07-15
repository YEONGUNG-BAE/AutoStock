from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.forward_monthly_observation import (  # noqa: E402
    FORWARD_CANDIDATE_ALLOCATOR_VERSION,
)
from backtest_engine.forward_monthly_observation_cli import main  # noqa: E402
import backtest_engine.forward_monthly_observation as observer  # noqa: E402

HEADER = "date,as_of,symbol,market,close_adjusted,source_name"


def _write_dataset(data_root: Path, periods: tuple[str, ...]) -> None:
    definitions = (
        ("sp500tr_monthly.csv", "SP500TR", "US", 100),
        ("kospi_monthly.csv", "KOSPI", "KR", 200),
        ("gld_monthly.csv", "GLD", "US", 50),
        ("usdkrw_monthly.csv", "USDKRW", "FX", 1300),
    )
    for filename, symbol, market, start in definitions:
        rows = []
        for index, period in enumerate(periods):
            rows.append(
                f"{period}-28,{period}-28T23:00:00+00:00,"
                f"{symbol},{market},{start + index},cli_synthetic_source"
            )
        path = data_root / "monthly" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _git_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_cli_prepare_and_finalize_two_stage_workflow(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    output_root = tmp_path / "forward-output"
    baseline = _git_repo(repo_root)
    _write_dataset(data_root, ("2026-04", "2026-05", "2026-06", "2026-07"))

    prepare_code = main(
        [
            "prepare",
            "--repo-root",
            str(repo_root),
            "--data-root",
            str(data_root),
            "--output-root",
            str(output_root),
            "--report-month",
            "2026-08",
            "--expected-git-main",
            baseline,
            "--candidate-allocator-version",
            FORWARD_CANDIDATE_ALLOCATOR_VERSION,
        ]
    )
    prepare_summary = json.loads(capsys.readouterr().out)
    assert prepare_code == 0
    assert prepare_summary["operation"] == "prepare"
    assert prepare_summary["observation_index"] == "1 of 12"

    _write_dataset(
        data_root,
        ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08"),
    )

    class _CompletedObservationDate(date):
        @classmethod
        def today(cls) -> date:
            return date(2026, 9, 1)

    monkeypatch.setattr(observer, "date", _CompletedObservationDate)
    finalize_code = main(
        [
            "finalize",
            "--repo-root",
            str(repo_root),
            "--data-root",
            str(data_root),
            "--output-root",
            str(output_root),
            "--decision-snapshot",
            prepare_summary["snapshot_path"],
            "--expected-git-main",
            baseline,
            "--candidate-allocator-version",
            FORWARD_CANDIDATE_ALLOCATOR_VERSION,
        ]
    )
    finalize_summary = json.loads(capsys.readouterr().out)
    assert finalize_code == 0
    assert finalize_summary["operation"] == "finalize"
    assert finalize_summary["evidence_status"] == "PENDING_FULL_WINDOW"
    assert Path(finalize_summary["metrics_path"]).parent == output_root.resolve()


def test_cli_reports_sanitized_contract_error(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    output_root = tmp_path / "forward-output"
    baseline = _git_repo(repo_root)
    _write_dataset(data_root, ("2026-06", "2026-07"))

    code = main(
        [
            "prepare",
            "--repo-root",
            str(repo_root),
            "--data-root",
            str(data_root),
            "--output-root",
            str(output_root),
            "--report-month",
            "2026-08",
            "--expected-git-main",
            baseline,
            "--candidate-allocator-version",
            "changed",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload == {
        "message": "candidate allocator version changed.",
        "status": "ERROR",
    }
