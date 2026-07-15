from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import backtest_engine.forward_monthly_observation as observer  # noqa: E402
from backtest_engine.forward_monthly_observation import (  # noqa: E402
    FORWARD_CANDIDATE_ALLOCATOR_VERSION,
    FORWARD_CANDIDATE_STATE_POLICY,
    FORWARD_IMPLEMENTED_US60_POLICY,
    FORWARD_PRODUCT_RELATIVE_V1_POLICY,
    ForwardDataQualityError,
    ForwardFrequencyAlignmentError,
    ForwardNavSanityError,
    ForwardObservationError,
    ForwardSnapshotIntegrityError,
    finalize_forward_monthly_observation,
    prepare_forward_monthly_observation,
)

BASELINE = "a" * 40
HEADER = "date,as_of,symbol,market,close_adjusted,source_name"


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    output_root = tmp_path / "forward-output"
    repo_root.mkdir()
    data_root.mkdir()
    return repo_root, data_root, output_root


def _write_csv(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _write_dataset(data_root: Path, periods: tuple[str, ...]) -> None:
    definitions = (
        ("sp500tr_monthly.csv", "SP500TR", "US", 100),
        ("kospi_monthly.csv", "KOSPI", "KR", 200),
        ("gld_monthly.csv", "GLD", "US", 50),
        ("usdkrw_monthly.csv", "USDKRW", "FX", 1300),
    )
    for filename, symbol, market, start in definitions:
        rows: list[str] = []
        for index, period in enumerate(periods):
            year, month = (int(part) for part in period.split("-"))
            value = start + (index * (10 if symbol == "SP500TR" else 2))
            rows.append(
                f"{year:04d}-{month:02d}-28,"
                f"{year:04d}-{month:02d}-28T23:00:00+00:00,"
                f"{symbol},{market},{value},synthetic_source_secret"
            )
        _write_csv(data_root / "monthly" / filename, rows)


def _prepare(
    tmp_path: Path,
    *,
    periods: tuple[str, ...] = ("2026-04", "2026-05", "2026-06", "2026-07"),
) -> tuple[Path, Path, Path, observer.ForwardPrepareResult]:
    repo_root, data_root, output_root = _roots(tmp_path)
    _write_dataset(data_root, periods)
    result = prepare_forward_monthly_observation(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        report_month="2026-08",
        expected_git_main=BASELINE,
        candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
        observed_git_head=BASELINE,
        today=date(2026, 7, 15),
    )
    return repo_root, data_root, output_root, result


def _finalize(
    *,
    repo_root: Path,
    data_root: Path,
    output_root: Path,
    snapshot_path: str,
) -> observer.ForwardFinalizeResult:
    return finalize_forward_monthly_observation(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        decision_snapshot_path=Path(snapshot_path),
        expected_git_main=BASELINE,
        candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
        observed_git_head=BASELINE,
        today=date(2026, 9, 1),
    )


def test_prepare_excludes_observation_month_and_freezes_frozen_identities(
    tmp_path: Path,
) -> None:
    repo_root, _, output_root, result = _prepare(tmp_path)

    assert not Path(result.snapshot_path).is_relative_to(repo_root)
    assert Path(result.snapshot_path).parent == output_root.resolve()
    assert result.snapshot.report_month == "2026-08"
    assert result.snapshot.decision_cutoff_period == "2026-07"
    assert result.snapshot.observation_index == "1 of 12"
    assert result.snapshot.candidate_allocator_version == FORWARD_CANDIDATE_ALLOCATOR_VERSION
    assert result.snapshot.candidate_state_policy == FORWARD_CANDIDATE_STATE_POLICY
    assert result.snapshot.implemented_us60_static_policy == FORWARD_IMPLEMENTED_US60_POLICY
    assert result.snapshot.product_relative_v1_neutral_policy == FORWARD_PRODUCT_RELATIVE_V1_POLICY
    assert [str(item.weight) for item in result.snapshot.candidate_target_weights] == [
        "0.70",
        "0.15",
        "0.10",
        "0.05",
    ]


def test_prepare_rejects_invalid_cutoff(tmp_path: Path) -> None:
    repo_root, data_root, output_root = _roots(tmp_path)
    _write_dataset(data_root, ("2026-04", "2026-05", "2026-06"))

    with pytest.raises(ForwardObservationError, match="decision_cutoff_period"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
            observed_git_head=BASELINE,
        )


def test_prepare_rejects_observation_month_outcome(tmp_path: Path) -> None:
    repo_root, data_root, output_root = _roots(tmp_path)
    _write_dataset(
        data_root,
        ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08"),
    )

    with pytest.raises(ForwardObservationError, match="observation-month outcome"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
            observed_git_head=BASELINE,
        )


def test_prepare_is_deterministic_and_refuses_unsafe_overwrite(tmp_path: Path) -> None:
    repo_root, data_root, output_root, first = _prepare(tmp_path)
    first_bytes = Path(first.snapshot_path).read_bytes()

    with pytest.raises(ForwardObservationError, match="refusing to overwrite"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
            observed_git_head=BASELINE,
        )

    second = prepare_forward_monthly_observation(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        report_month="2026-08",
        expected_git_main=BASELINE,
        candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
        observed_git_head=BASELINE,
        safe_overwrite=True,
        today=date(2026, 7, 31),
    )
    assert Path(second.snapshot_path).read_bytes() == first_bytes
    assert second.snapshot_sha256 == first.snapshot_sha256

    with pytest.raises(ForwardObservationError, match="deadline"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
            observed_git_head=BASELINE,
            safe_overwrite=True,
            today=date(2026, 8, 1),
        )


def test_prepare_rejects_output_inside_repository(tmp_path: Path) -> None:
    repo_root, data_root, _ = _roots(tmp_path)
    _write_dataset(data_root, ("2026-04", "2026-05", "2026-06", "2026-07"))

    with pytest.raises(ForwardObservationError, match="outside repo_root"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=repo_root / "evidence",
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
            observed_git_head=BASELINE,
        )


def test_snapshot_modification_is_detected(tmp_path: Path) -> None:
    repo_root, data_root, output_root, prepared = _prepare(tmp_path)
    path = Path(prepared.snapshot_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["candidate_target_weights"][0]["weight"] = "0.69"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ForwardSnapshotIntegrityError, match="digest mismatch"):
        _finalize(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            snapshot_path=str(path),
        )


def test_finalize_requires_original_snapshot(tmp_path: Path) -> None:
    repo_root, data_root, output_root, _ = _prepare(tmp_path)

    with pytest.raises(ForwardSnapshotIntegrityError, match="required"):
        _finalize(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            snapshot_path=str(output_root / "missing.backtest.json"),
        )


def test_finalize_uses_frozen_decision_and_evaluates_exactly_one_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root, data_root, output_root, prepared = _prepare(tmp_path)
    _write_dataset(
        data_root,
        (
            "2026-04",
            "2026-05",
            "2026-06",
            "2026-07",
            "2026-08",
            "2026-09",
        ),
    )

    def _must_not_recompute() -> None:
        raise AssertionError("FINALIZE must not call the allocator")

    monkeypatch.setattr(observer, "allocate_rules_v2_target_weights", _must_not_recompute)
    result = _finalize(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        snapshot_path=prepared.snapshot_path,
    )

    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert result.evidence_status == "PENDING_FULL_WINDOW"
    assert result.metrics["evidence_status"] != "PASS"
    assert manifest["evaluated_observation_count"] == 1
    assert result.metrics["nav_sanity_status"] == "PASS"
    assert result.metrics["dataset_sanity_status"] == "PASS"
    assert result.metrics["frequency_alignment_status"] == "PASS"
    assert result.metrics["static_comparator_separation_status"] == "PASS"


def test_finalize_missing_observation_is_pending(tmp_path: Path) -> None:
    repo_root, data_root, output_root, prepared = _prepare(tmp_path)

    result = _finalize(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        snapshot_path=prepared.snapshot_path,
    )

    assert result.evidence_status == "PENDING_MONTHLY_OBSERVATION"
    assert result.metrics["candidate_monthly_return"] is None
    assert result.metrics["candidate_cumulative_return_to_date"] is None


def test_finalize_does_not_accept_observation_before_month_is_complete(
    tmp_path: Path,
) -> None:
    repo_root, data_root, output_root, prepared = _prepare(tmp_path)
    _write_dataset(
        data_root,
        ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08"),
    )

    result = finalize_forward_monthly_observation(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        decision_snapshot_path=Path(prepared.snapshot_path),
        expected_git_main=BASELINE,
        candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
        observed_git_head=BASELINE,
        today=date(2026, 8, 31),
    )

    assert result.evidence_status == "PENDING_MONTHLY_OBSERVATION"
    assert result.metrics["candidate_monthly_return"] is None


@pytest.mark.parametrize(
    ("validator_name", "exception", "expected_status", "sanity_field"),
    (
        (
            "_validate_finalize_dataset",
            ForwardDataQualityError("synthetic data failure"),
            "BLOCKED_DATA_QUALITY",
            "dataset_sanity_status",
        ),
        (
            "_validate_nav_sanity",
            ForwardNavSanityError("synthetic NAV failure"),
            "BLOCKED_NAV_SANITY",
            "nav_sanity_status",
        ),
        (
            "_validate_frequency",
            ForwardFrequencyAlignmentError("synthetic frequency failure"),
            "BLOCKED_FREQUENCY_ALIGNMENT",
            "frequency_alignment_status",
        ),
        (
            "_validate_static_comparator_separation",
            observer.ForwardStaticComparatorSeparationError(
                "synthetic comparator separation failure"
            ),
            "BLOCKED_DATA_QUALITY",
            "static_comparator_separation_status",
        ),
    ),
)
def test_finalize_maps_sanity_failures_to_blocked_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator_name: str,
    exception: Exception,
    expected_status: str,
    sanity_field: str,
) -> None:
    repo_root, data_root, output_root, prepared = _prepare(tmp_path)
    _write_dataset(
        data_root,
        ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08"),
    )

    def _fail(*args: object, **kwargs: object) -> None:
        raise exception

    monkeypatch.setattr(observer, validator_name, _fail)
    result = _finalize(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        snapshot_path=prepared.snapshot_path,
    )

    assert result.evidence_status == expected_status
    assert result.metrics[sanity_field] == "BLOCKED"


def test_static_comparators_remain_separate_and_outputs_are_sanitized(
    tmp_path: Path,
) -> None:
    repo_root, data_root, output_root, prepared = _prepare(tmp_path)
    _write_dataset(
        data_root,
        ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08"),
    )
    result = _finalize(
        repo_root=repo_root,
        data_root=data_root,
        output_root=output_root,
        snapshot_path=prepared.snapshot_path,
    )

    assert result.metrics["implemented_us60_static_monthly_return"] != (
        result.metrics["product_relative_v1_neutral_monthly_return"]
    )
    for artifact in (
        Path(prepared.snapshot_path),
        Path(result.metrics_path),
        Path(result.manifest_path),
    ):
        text = artifact.read_text(encoding="utf-8")
        assert "source_name" not in text
        assert "synthetic_source_secret" not in text
        assert '"1300"' not in text
        assert "config.toml" not in text


def test_prepare_rejects_changed_allocator_and_git_identity(tmp_path: Path) -> None:
    repo_root, data_root, output_root = _roots(tmp_path)
    _write_dataset(data_root, ("2026-04", "2026-05", "2026-06", "2026-07"))

    with pytest.raises(ForwardObservationError, match="allocator version changed"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version="changed",
            observed_git_head=BASELINE,
        )
    with pytest.raises(ForwardObservationError, match="local HEAD"):
        prepare_forward_monthly_observation(
            repo_root=repo_root,
            data_root=data_root,
            output_root=output_root,
            report_month="2026-08",
            expected_git_main=BASELINE,
            candidate_allocator_version=FORWARD_CANDIDATE_ALLOCATOR_VERSION,
            observed_git_head="b" * 40,
        )
