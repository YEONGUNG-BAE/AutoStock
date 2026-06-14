"""RTM-7c.4l — explicit freshness-qualified activation candidate preflight tests."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition import activation_candidate_freshness_preflight as freshness_mod
from composition import activation_candidate_final_preflight as final_mod
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
    freshness_qualify_activation_candidate,
)
from composition.activation_candidate_final_preflight import (
    ActivationCandidateFinalPreflightOutcome,
    final_preflight_verified_activation_candidate,
)
from composition.paper_fast_loop import MachineCheckOutcome, precheck_runtime
from composition.receipt_freshness_policy import (
    ReceiptFreshnessOutcome,
    ReceiptFreshnessPolicy,
    evaluate_receipt_freshness,
)
from composition.receipt_time_assessment import ReceiptTimeAssessmentOutcome
from composition.verified_precheck_receipt import (
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)
from composition.paper_fast_loop_artifacts import PAPER_FAST_LOOP_ARTIFACT_NAMES
from config.settings import RuntimePaperFastLoopSettings

import test_activation_candidate_final_preflight as fp_helper
import test_paper_fast_loop_composition as pfl_helper
import test_precheck_receipt_verifier as vrf_helper

_NOW = fp_helper._NOW
_FUTURE = fp_helper._FUTURE
_PAST = fp_helper._PAST


def _settings(tmp_path: Path, **overrides: Any) -> RuntimePaperFastLoopSettings:
    return fp_helper._settings(tmp_path, **overrides)


def _pass_receipt(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> dict[str, Any]:
    return fp_helper._pass_receipt_for_seeded_stack(tmp_path, settings)


def _policy(max_age: int) -> ReceiptFreshnessPolicy:
    return ReceiptFreshnessPolicy(max_age_microseconds=max_age)


def _qualify(
    tmp_path: Path,
    *,
    receipt: object,
    now: datetime = _NOW,
    max_age: int = 1_000_000_000,
    settings: RuntimePaperFastLoopSettings | None = None,
) -> Any:
    resolved_settings = settings or _settings(tmp_path)
    return freshness_qualify_activation_candidate(
        settings=resolved_settings,
        receipt_payload=receipt,
        now=now,
        policy=_policy(max_age),
        base_dir=tmp_path,
    )


def _assert_posture(result: Any, *, freshness_policy_evaluated: bool) -> None:
    assert result.activation_authorized is False
    assert result.runtime_activation_outcome == "no_go"
    assert result.explicit_operator_approval_required is True
    assert result.writers_stopped_manual_confirmation_required is True
    assert result.freshness_policy_evaluated is freshness_policy_evaluated


# --- invalid policy early short-circuit ---


class _RaisingTzInfo(__import__("datetime").tzinfo):
    def utcoffset(self, dt: datetime | None) -> Any:
        raise ValueError("boom")

    def tzname(self, dt: datetime | None) -> str | None:
        return None

    def dst(self, dt: datetime | None) -> Any:
        return None


class _NoneOffsetTzInfo(__import__("datetime").tzinfo):
    def utcoffset(self, dt: datetime | None) -> Any:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None

    def dst(self, dt: datetime | None) -> Any:
        return None


_INVALID_NOW_CASES = [
    pytest.param(None, id="none"),
    pytest.param("2026-06-16T00:30:00+09:00", id="str"),
    pytest.param(1718498400, id="int"),
    pytest.param(datetime(2026, 6, 16, 0, 30), id="naive"),  # noqa: DTZ001
    pytest.param(
        datetime(2026, 6, 16, 0, 30, tzinfo=_RaisingTzInfo()),
        id="raising_tz",
    ),
    pytest.param(
        datetime(2026, 6, 16, 0, 30, tzinfo=_NoneOffsetTzInfo()),
        id="none_offset",
    ),
]

@pytest.mark.parametrize(
    "bad_policy",
    [
        None,
        object(),
        ReceiptFreshnessPolicy(max_age_microseconds=-1),
        ReceiptFreshnessPolicy(max_age_microseconds=True),  # type: ignore[arg-type]
    ],
    ids=["none", "object", "negative_max", "bool_max"],
)
def test_invalid_policy_short_circuits_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_policy: object,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)

    def _fail_snapshot(_: object) -> object:
        raise AssertionError("snapshot must not run for invalid policy")

    def _fail_final(*_a: object, **_k: object) -> object:
        raise AssertionError("final preflight must not run for invalid policy")

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("freshness evaluator must not run for invalid policy")

    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _fail_snapshot)
    monkeypatch.setattr(freshness_mod, "final_preflight_verified_activation_candidate", _fail_final)
    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)

    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=_NOW,
        policy=bad_policy,  # type: ignore[arg-type]
        base_dir=tmp_path,
    )
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.NO_GO
    assert result.reasons == ("candidate_freshness_policy_invalid",)
    assert result.final_preflight_result is None
    assert result.freshness_evaluation is None
    _assert_posture(result, freshness_policy_evaluated=False)


def test_policy_subclass_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SubPolicy(ReceiptFreshnessPolicy):
        pass

    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)

    def _fail_snapshot(_: object) -> object:
        raise AssertionError("snapshot must not run for policy subclass")

    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _fail_snapshot)

    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=_NOW,
        policy=SubPolicy(max_age_microseconds=100),
        base_dir=tmp_path,
    )
    assert result.reasons == ("candidate_freshness_policy_invalid",)


def test_invalid_policy_beats_invalid_now(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=None,  # type: ignore[arg-type]
        policy=ReceiptFreshnessPolicy(max_age_microseconds=-1),
        base_dir=tmp_path,
    )
    assert result.reasons == ("candidate_freshness_policy_invalid",)
    _assert_posture(result, freshness_policy_evaluated=False)


@pytest.mark.parametrize("bad_now", _INVALID_NOW_CASES)
def test_invalid_now_short_circuits_before_receipt_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_now: object,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    policy = _policy(100)

    def _fail_receipt_snapshot(_: object) -> object:
        raise AssertionError("receipt snapshot must not run for invalid now")

    def _fail_final(*_a: object, **_k: object) -> object:
        raise AssertionError("final preflight must not run for invalid now")

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("freshness evaluator must not run for invalid now")

    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _fail_receipt_snapshot)
    monkeypatch.setattr(freshness_mod, "final_preflight_verified_activation_candidate", _fail_final)
    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)

    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=bad_now,  # type: ignore[arg-type]
        policy=policy,
        base_dir=tmp_path,
    )
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.NO_GO
    assert result.reasons == ("candidate_invalid_now",)
    assert result.receipt_sha256 is None
    assert result.market is None
    assert result.symbol is None
    assert result.final_preflight_result is None
    assert result.freshness_evaluation is None
    _assert_posture(result, freshness_policy_evaluated=False)


def test_invalid_now_matches_final_preflight_wrapper_reason(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    wrapper = fp_helper.final_preflight_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=None,  # type: ignore[arg-type]
        base_dir=tmp_path,
    )
    qualified = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=None,  # type: ignore[arg-type]
        policy=_policy(100),
        base_dir=tmp_path,
    )
    assert wrapper.reasons == ("candidate_invalid_now",)
    assert qualified.reasons == ("candidate_invalid_now",)


def test_valid_policy_invalid_now_policy_snapshot_once_receipt_snapshot_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    policy_calls: list[int] = []
    receipt_calls: list[int] = []
    real_policy_snapshot = freshness_mod.snapshot_receipt_freshness_policy

    def _spy_policy(policy: object) -> ReceiptFreshnessPolicy | None:
        policy_calls.append(1)
        return real_policy_snapshot(policy)

    def _fail_receipt_snapshot(_: object) -> object:
        receipt_calls.append(1)
        raise AssertionError("receipt snapshot must not run for invalid now")

    monkeypatch.setattr(freshness_mod, "snapshot_receipt_freshness_policy", _spy_policy)
    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _fail_receipt_snapshot)

    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=datetime(2026, 6, 16),  # naive  # noqa: DTZ001
        policy=_policy(100),
        base_dir=tmp_path,
    )
    assert result.reasons == ("candidate_invalid_now",)
    assert len(policy_calls) == 1
    assert receipt_calls == []


# --- policy snapshot + mutation ---


def test_policy_mutation_during_final_core_pass_uses_initial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    policy = ReceiptFreshnessPolicy(max_age_microseconds=100)
    now = _NOW + timedelta(microseconds=50)
    real_reval = final_mod.revalidate_verified_activation_candidate

    def _mutating_reval(*args: Any, **kwargs: Any) -> Any:
        object.__setattr__(policy, "max_age_microseconds", 0)
        return real_reval(*args, **kwargs)

    monkeypatch.setattr(final_mod, "revalidate_verified_activation_candidate", _mutating_reval)
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=now,
        policy=policy,
        base_dir=tmp_path,
    )
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS
    assert result.freshness_evaluation is not None
    assert result.freshness_evaluation.max_age_microseconds == 100


def test_policy_mutation_during_final_core_stale_uses_initial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    policy = ReceiptFreshnessPolicy(max_age_microseconds=0)
    now = _NOW + timedelta(microseconds=50)
    real_reval = final_mod.revalidate_verified_activation_candidate

    def _mutating_reval(*args: Any, **kwargs: Any) -> Any:
        object.__setattr__(policy, "max_age_microseconds", 100)
        return real_reval(*args, **kwargs)

    monkeypatch.setattr(final_mod, "revalidate_verified_activation_candidate", _mutating_reval)
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=now,
        policy=policy,
        base_dir=tmp_path,
    )
    assert result.reasons == ("candidate_receipt_stale",)
    assert result.freshness_evaluation is not None
    assert result.freshness_evaluation.max_age_microseconds == 0


def test_policy_mutation_after_result_does_not_change_nested_evaluation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    policy = ReceiptFreshnessPolicy(max_age_microseconds=100)
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=_NOW,
        policy=policy,
        base_dir=tmp_path,
    )
    frozen_eval = result.freshness_evaluation
    object.__setattr__(policy, "max_age_microseconds", 0)
    assert result.freshness_evaluation is frozen_eval
    assert result.freshness_evaluation.max_age_microseconds == 100


def test_evaluator_receives_snapshot_not_caller_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    caller_policy = ReceiptFreshnessPolicy(max_age_microseconds=100)
    seen: list[ReceiptFreshnessPolicy] = []

    def _spy_eval(*, time_assessment: Any, policy: ReceiptFreshnessPolicy) -> Any:
        seen.append(policy)
        return evaluate_receipt_freshness(time_assessment=time_assessment, policy=policy)

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _spy_eval)
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=_NOW,
        policy=caller_policy,
        base_dir=tmp_path,
    )
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS
    assert len(seen) == 1
    assert type(seen[0]) is ReceiptFreshnessPolicy
    assert seen[0] is not caller_policy
    assert seen[0].max_age_microseconds == 100


def test_policy_snapshot_builds_once_on_valid_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    calls: list[int] = []
    real = freshness_mod.snapshot_receipt_freshness_policy

    def _spy(policy: object) -> ReceiptFreshnessPolicy | None:
        calls.append(1)
        return real(policy)

    monkeypatch.setattr(freshness_mod, "snapshot_receipt_freshness_policy", _spy)
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS
    assert len(calls) == 1


# --- fresh boundary ---


@pytest.mark.parametrize(
    ("age_us", "max_age", "expected_outcome", "expected_reasons", "policy_evaluated"),
    [
        (0, 0, ActivationCandidateFreshnessPreflightOutcome.PASS, (), True),
        (100, 100, ActivationCandidateFreshnessPreflightOutcome.PASS, (), True),
        (99, 100, ActivationCandidateFreshnessPreflightOutcome.PASS, (), True),
        (101, 100, ActivationCandidateFreshnessPreflightOutcome.NO_GO, ("candidate_receipt_stale",), True),
        (1, 0, ActivationCandidateFreshnessPreflightOutcome.NO_GO, ("candidate_receipt_stale",), True),
    ],
)
def test_freshness_boundary_matrix(
    tmp_path: Path,
    age_us: int,
    max_age: int,
    expected_outcome: ActivationCandidateFreshnessPreflightOutcome,
    expected_reasons: tuple[str, ...],
    policy_evaluated: bool,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    now = _NOW + timedelta(microseconds=age_us)
    result = _qualify(tmp_path, receipt=receipt, now=now, max_age=max_age, settings=settings)
    assert result.outcome is expected_outcome
    assert result.reasons == expected_reasons
    _assert_posture(result, freshness_policy_evaluated=policy_evaluated)
    if expected_outcome is ActivationCandidateFreshnessPreflightOutcome.PASS:
        assert result.freshness_evaluation is not None
        assert result.freshness_evaluation.outcome is ReceiptFreshnessOutcome.FRESH


# --- final preflight NO_GO precedence ---


def test_invalid_receipt_preserves_reason_and_skips_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("evaluator must not run on invalid receipt")

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)
    result = _qualify(tmp_path, receipt={"schema_version": 1}, settings=settings)
    assert result.reasons == ("candidate_receipt_invalid",)
    assert result.freshness_evaluation is None
    _assert_posture(result, freshness_policy_evaluated=False)


def test_future_receipt_preserves_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("evaluator must not run on future receipt")

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)
    result = _qualify(tmp_path, receipt=receipt, now=_PAST, settings=settings)
    assert result.reasons == ("candidate_receipt_time_in_future",)
    assert result.final_preflight_result is not None
    assert result.freshness_evaluation is None
    _assert_posture(result, freshness_policy_evaluated=False)


def test_expired_snapshot_preserves_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("evaluator must not run on expired snapshot")

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)
    result = _qualify(tmp_path, receipt=receipt, now=_FUTURE, settings=settings)
    assert "candidate_current_precheck:execution_inputs_expired" in result.reasons
    assert result.freshness_evaluation is None
    _assert_posture(result, freshness_policy_evaluated=False)


def test_config_mismatch_preserves_reason(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt(symbol="000660")
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.reasons == ("candidate_symbol_mismatch",)
    _assert_posture(result, freshness_policy_evaluated=False)


def test_post_revalidation_drift_preserves_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    artifact = PAPER_FAST_LOOP_ARTIFACT_NAMES[0]
    import composition.sqlite_inspector as insp

    real_fp = insp.fingerprint_artifact
    seen: dict[str, int] = {}

    def _drift_fp(path: str | Path, *, name: str, is_sqlite: bool) -> Any:
        fp = real_fp(path, name=name, is_sqlite=is_sqlite)
        if name != artifact:
            return fp
        seen[name] = seen.get(name, 0) + 1
        if seen[name] <= 2:
            return fp
        from composition.sqlite_inspector import ArtifactFingerprint

        return ArtifactFingerprint(
            name=fp.name,
            present=fp.present,
            is_regular_file=fp.is_regular_file,
            size=(fp.size or 0) + 9,
            sha256="ef" * 32,
            user_version=fp.user_version,
            sidecar_suffixes=fp.sidecar_suffixes,
        )

    monkeypatch.setattr(insp, "fingerprint_artifact", _drift_fp)

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("evaluator must not run on post-revalidation drift")

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.reasons == (f"candidate_post_revalidation_artifact_drift:{artifact}",)
    _assert_posture(result, freshness_policy_evaluated=False)


# --- verifier / snapshot single observation ---


def test_verifier_called_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    calls: list[int] = []
    real = verify_and_snapshot_precheck_receipt

    def _spy(payload: object) -> object:
        calls.append(1)
        return real(payload)

    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _spy)
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS
    assert len(calls) == 1


def test_verified_core_calls_verifier_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    snapshot = verify_and_snapshot_precheck_receipt(receipt)
    assert snapshot.receipt is not None

    import composition.precheck_receipt_verifier as vrf_mod

    def _fail_verifier(_: object) -> object:
        raise AssertionError("verified core must not call verifier")

    monkeypatch.setattr(vrf_mod, "verify_runtime_precheck_receipt_payload", _fail_verifier)
    result = final_preflight_verified_activation_candidate(
        settings=settings,
        receipt=snapshot.receipt,
        now=_NOW,
        base_dir=tmp_path,
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.PASS


def test_raw_payload_mutation_after_snapshot_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    original_hash = receipt["receipt_sha256"]
    real_reval = final_mod.revalidate_verified_activation_candidate

    def _mutating_reval(*args: Any, **kwargs: Any) -> Any:
        result = real_reval(*args, **kwargs)
        receipt["symbol"] = "000660"
        receipt["fingerprints_after"][0]["sha256"] = "00" * 32
        receipt["receipt_sha256"] = "ff" * 32
        return result

    monkeypatch.setattr(final_mod, "revalidate_verified_activation_candidate", _mutating_reval)
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS
    assert result.receipt_sha256 == original_hash
    assert result.symbol == settings.symbol


def test_time_assessment_object_identity_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    seen: list[ReceiptTimeAssessment | None] = []

    def _spy_eval(*, time_assessment: Any, policy: ReceiptFreshnessPolicy) -> Any:
        seen.append(time_assessment)
        return evaluate_receipt_freshness(time_assessment=time_assessment, policy=policy)

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _spy_eval)
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.final_preflight_result is not None
    assert result.final_preflight_result.receipt_time_assessment is seen[0]


# --- verified core exposes receipt_time_assessment ---


def test_verified_core_pass_carries_time_assessment(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    snapshot = verify_and_snapshot_precheck_receipt(receipt)
    assert snapshot.receipt is not None
    result = final_preflight_verified_activation_candidate(
        settings=settings,
        receipt=snapshot.receipt,
        now=_NOW,
        base_dir=tmp_path,
    )
    assert result.receipt_time_assessment is not None
    assert result.receipt_time_assessment.outcome is ReceiptTimeAssessmentOutcome.VALID


def test_verified_core_revalidation_short_circuit_time_assessment_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt(symbol="000660")
    snapshot = verify_and_snapshot_precheck_receipt(receipt)
    assert snapshot.receipt is not None
    result = final_preflight_verified_activation_candidate(
        settings=settings,
        receipt=snapshot.receipt,
        now=_NOW,
        base_dir=tmp_path,
    )
    assert result.receipt_time_assessment is None


# --- isolation ---


def test_module_reads_no_clock() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "activation_candidate_freshness_preflight.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "datetime.utcnow", "time.time", "time.monotonic"):
        assert forbidden not in source


def test_module_has_no_config_env_default() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "activation_candidate_freshness_preflight.py"
    ).read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "load_settings" not in source
    assert "DEFAULT" not in source


def test_invalid_policy_no_sqlite_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    uris: list[str] = []
    real_connect = sqlite3.connect

    def _spy_connect(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        uris.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=_NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=-1),
        base_dir=tmp_path,
    )
    assert result.reasons == ("candidate_freshness_policy_invalid",)
    assert uris == []


def test_final_preflight_wrapper_still_policy_neutral(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    result = fp_helper.final_preflight_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=_NOW,
        base_dir=tmp_path,
    )
    assert result.freshness_policy_evaluated is False


def test_final_preflight_module_does_not_import_freshness_preflight() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "activation_candidate_final_preflight.py"
    ).read_text(encoding="utf-8")
    assert "activation_candidate_freshness_preflight" not in source
    assert "freshness_qualify" not in source


def test_evaluator_defensive_no_go_maps_to_candidate_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)

    from composition.receipt_freshness_policy import ReceiptFreshnessEvaluation

    def _bogus_eval(*_a: object, **_k: object) -> ReceiptFreshnessEvaluation:
        return ReceiptFreshnessEvaluation(
            outcome=ReceiptFreshnessOutcome.NO_GO,
            reasons=("freshness_time_assessment_invalid",),
            receipt_age_microseconds=None,
            max_age_microseconds=100,
            freshness_policy_evaluated=False,
            activation_authorized=False,
            runtime_activation_outcome="no_go",
        )

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _bogus_eval)
    result = _qualify(tmp_path, receipt=receipt, settings=settings)
    assert result.reasons == ("candidate_freshness_evaluation_invalid",)
    _assert_posture(result, freshness_policy_evaluated=False)


@pytest.mark.parametrize(
    ("policy_ok", "now_ok", "receipt_ok", "expected_reason"),
    [
        (False, False, True, "candidate_freshness_policy_invalid"),
        (True, False, True, "candidate_invalid_now"),
        (True, True, False, "candidate_receipt_invalid"),
    ],
    ids=["invalid_policy", "invalid_now", "invalid_receipt"],
)
def test_reason_precedence_matrix_early_short_circuits(
    tmp_path: Path,
    policy_ok: bool,
    now_ok: bool,
    receipt_ok: bool,
    expected_reason: str,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings) if receipt_ok else {"schema_version": 1}
    policy = _policy(100) if policy_ok else ReceiptFreshnessPolicy(max_age_microseconds=-1)
    now = _NOW if now_ok else None
    result = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=now,  # type: ignore[arg-type]
        policy=policy,
        base_dir=tmp_path,
    )
    assert result.reasons == (expected_reason,)


# --- RTM-7c.4m CLI: explicit freshness-qualified preflight ---


import importlib.util
import io
import json

import config.settings as _settings_mod

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_KST = cli._KST
_SYMBOL = pfl_helper._SYMBOL


def _stdin_bytes(data: bytes) -> object:
    class _Stdin:
        buffer = io.BytesIO(data)

    return _Stdin()


def _write_config(tmp_path: Path, *, enabled: bool = True, symbol: str = _SYMBOL) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[runtime.paper_fast_loop]
enabled = {str(enabled).lower()}
market = "KR"
symbol = "{symbol}"
""",
        encoding="utf-8",
    )
    return config_path


class _FixedClock:
    @staticmethod
    def now(tz: object = None) -> datetime:
        return _NOW.astimezone(_KST) if tz is not None else _NOW


class _AgedClock:
    """Pins CLI clock to receipt checked_at + ``offset_us`` microseconds."""

    def __init__(self, offset_us: int) -> None:
        self._offset_us = offset_us

    def now(self, tz: object = None) -> datetime:
        instant = _NOW + timedelta(microseconds=self._offset_us)
        return instant.astimezone(_KST) if tz is not None else instant


class _FutureClock:
    @staticmethod
    def now(tz: object = None) -> datetime:
        return _FUTURE.astimezone(_KST) if tz is not None else _FUTURE


class _PastClock:
    @staticmethod
    def now(tz: object = None) -> datetime:
        return _PAST.astimezone(_KST) if tz is not None else _PAST


def _freshness_argv(config_path: Path, max_age: str | None = None) -> list[str]:
    argv = [
        "--config",
        str(config_path),
        "--freshness-preflight-activation-candidate",
        "--json",
    ]
    if max_age is not None:
        argv.extend(["--max-age-microseconds", max_age])
    return argv


def _run_freshness_cli(
    argv: list[str], receipt: dict[str, Any] | None, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    if receipt is not None:
        sys.stdin = _stdin_bytes(json.dumps(receipt).encode("utf-8"))  # type: ignore[assignment]
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def _assert_freshness_posture(payload: dict[str, Any], *, policy_evaluated: bool) -> None:
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["explicit_operator_approval_required"] is True
    assert payload["writers_stopped_manual_confirmation_required"] is True
    assert payload["freshness_policy_evaluated"] is policy_evaluated
    assert payload["credential_read"] is False
    assert payload["network_called"] is False
    assert payload["broker_called"] is False
    assert payload["operational_db_written"] is False
    assert payload["filesystem_written"] is False
    assert payload["runtime_file_created"] is False
    assert payload["mode"] == "freshness_preflight_activation_candidate"
    assert "config" not in payload


def test_freshness_cli_pass_at_age_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    code, payload = _run_freshness_cli(
        _freshness_argv(config_path, "100"), receipt, capsys
    )
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["reasons"] == []
    assert payload["receipt_age_microseconds"] == 0
    assert payload["max_age_microseconds"] == 100
    assert payload["final_preflight_outcome"] == "pass"
    _assert_freshness_posture(payload, policy_evaluated=True)


@pytest.mark.parametrize(
    ("offset_us", "max_age", "expect_pass"),
    [
        (100, 100, True),
        (99, 100, True),
        (101, 100, False),
        (0, 0, True),
        (1, 0, False),
    ],
)
def test_freshness_cli_age_boundary_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    offset_us: int,
    max_age: int,
    expect_pass: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _AgedClock(offset_us))
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    code, payload = _run_freshness_cli(
        _freshness_argv(config_path, str(max_age)), receipt, capsys
    )
    assert code == (0 if expect_pass else 1)
    assert payload["outcome"] == ("PASS" if expect_pass else "NO_GO")
    assert payload["receipt_age_microseconds"] == offset_us
    assert payload["max_age_microseconds"] == max_age
    if expect_pass:
        assert payload["reasons"] == []
        _assert_freshness_posture(payload, policy_evaluated=True)
    else:
        assert payload["reasons"] == ["candidate_receipt_stale"]
        _assert_freshness_posture(payload, policy_evaluated=True)


def test_freshness_cli_missing_max_age(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_freshness_cli(
        ["--freshness-preflight-activation-candidate", "--json"],
        None,
        capsys,
    )
    assert code == 1
    assert payload["reasons"] == ["freshness_policy_input_missing"]
    assert payload["max_age_microseconds"] is None
    _assert_freshness_posture(payload, policy_evaluated=False)


def test_freshness_cli_invalid_max_age_no_side_effects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    load_calls: list[str] = []

    def _spy_load(*_a: object, **_k: object) -> object:
        load_calls.append("load")
        raise AssertionError("config must not load on invalid max-age")

    monkeypatch.setattr(cli, "load_settings", _spy_load)

    class _RaisingDatetime:
        @staticmethod
        def now(tz: object = None) -> datetime:
            raise AssertionError("clock must not read on invalid max-age")

    monkeypatch.setattr(cli, "datetime", _RaisingDatetime)
    monkeypatch.setattr(_settings_mod, "os", type("_Os", (), {"environ": {}})())

    code, payload = _run_freshness_cli(
        ["--freshness-preflight-activation-candidate", "--max-age-microseconds", "-1", "--json"],
        None,
        capsys,
    )
    assert code == 1
    assert payload["reasons"] == ["freshness_policy_input_invalid"]
    assert stdin_reads == []
    assert load_calls == []


def test_freshness_cli_max_age_not_applicable_on_final_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run_freshness_cli(
        [
            "--final-preflight-activation-candidate",
            "--max-age-microseconds",
            "100",
            "--json",
        ],
        None,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reason_code"] == "freshness_policy_argument_not_applicable"


def test_freshness_cli_run_with_max_age_still_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_freshness_cli(
        ["--run", "--max-age-microseconds", "100", "--json"], None, capsys
    )
    assert code == 2
    assert payload["reason_code"] == "live_run_not_implemented"


def test_freshness_cli_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_freshness_cli(
        [
            "--freshness-preflight-activation-candidate",
            "--final-preflight-activation-candidate",
            "--max-age-microseconds",
            "100",
            "--json",
        ],
        vrf_helper._valid_receipt(),
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert "mutually exclusive" in payload["reason_code"]


@pytest.mark.parametrize(
    ("clock", "expected_reason"),
    [
        (_PastClock, "candidate_receipt_time_in_future"),
        (_FutureClock, "candidate_current_precheck:execution_inputs_expired"),
    ],
    ids=["future_receipt", "expired_snapshot"],
)
def test_freshness_cli_final_preflight_no_go_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: type,
    expected_reason: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", clock)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("freshness evaluator must not run on final NO_GO")

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)
    code, payload = _run_freshness_cli(
        _freshness_argv(config_path, "999999999999"), receipt, capsys
    )
    assert code == 1
    assert payload["freshness_policy_evaluated"] is False
    if expected_reason.startswith("candidate_current_precheck:"):
        assert any(r.startswith("candidate_current_precheck:") for r in payload["reasons"])
    else:
        assert payload["reasons"] == [expected_reason]


def test_freshness_cli_config_mismatch_no_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    receipt = vrf_helper._valid_receipt(symbol="000660")

    def _fail_eval(*_a: object, **_k: object) -> object:
        raise AssertionError("evaluator must not run on config mismatch")

    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _fail_eval)
    code, payload = _run_freshness_cli(
        _freshness_argv(config_path, "100"), receipt, capsys
    )
    assert code == 1
    assert payload["reasons"] == ["candidate_symbol_mismatch"]
    assert payload["freshness_policy_evaluated"] is False
    assert payload["max_age_microseconds"] == 100


class _NoEnvironAccess:
    _MSG = "freshness preflight must not read os.environ"

    def __getitem__(self, key: object) -> str:
        raise AssertionError(f"{self._MSG} (__getitem__ {key!r})")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"{self._MSG} (__contains__ {key!r})")

    def get(self, key: object, default: object = None) -> object:
        raise AssertionError(f"{self._MSG} (get {key!r})")

    def __iter__(self):
        raise AssertionError(f"{self._MSG} (__iter__)")

    def keys(self):
        raise AssertionError(f"{self._MSG} (keys)")

    def copy(self):
        raise AssertionError(f"{self._MSG} (copy)")


import os as _real_os  # noqa: E402


class _OsShim:
    environ = _NoEnvironAccess()

    def __getattr__(self, name: str) -> object:
        return getattr(_real_os, name)


def test_freshness_cli_zero_environ_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    monkeypatch.setattr(_settings_mod, "os", _OsShim())
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt(tmp_path, settings)
    code, payload = _run_freshness_cli(
        _freshness_argv(config_path, "100"), receipt, capsys
    )
    assert code == 0
    assert payload["outcome"] == "PASS"


def test_freshness_cli_env_substitution_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_settings_mod, "os", _OsShim())
    config_path = tmp_path / "config_env.toml"
    config_path.write_text(
        """
[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "005930"
ledger_path = "${LEDGER_PATH}/ledger.sqlite3"
""",
        encoding="utf-8",
    )
    receipt = vrf_helper._valid_receipt()
    code, payload = _run_freshness_cli(
        _freshness_argv(config_path, "100"), receipt, capsys
    )
    assert code == 1
    assert payload["reasons"] == ["config error: ConfigEnvironmentError"]


def test_freshness_cli_sanitizes_poison_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    poison_path = tmp_path / "poison_config.toml"
    poison_path.write_text(
        """
[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "005930"
""",
        encoding="utf-8",
    )
    receipt = vrf_helper._valid_receipt()
    receipt["reasons"] = ["/home/user/KIS_LIVE_APP_KEY/secret"]
    code, payload = _run_freshness_cli(
        _freshness_argv(poison_path.resolve(), "100"), receipt, capsys
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert "KIS_" not in combined
    assert "APP_SECRET" not in combined
    assert "/home/" not in combined
    assert str(poison_path) not in combined
    assert "Traceback" not in combined
    assert "ValueError(" not in combined
    assert json.dumps(receipt) not in combined


def test_freshness_cli_module_import_guard() -> None:
    source = _CLI_PATH.read_text(encoding="utf-8")
    for forbidden in ("socket", "websocket", "websockets", "http", "httpx", "urllib", "requests"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source