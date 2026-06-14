"""RTM-7c.4e — precheck receipt builder validation + pure verifier tests."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.paper_fast_loop import (
    InspectionOutcome,
    MachineCheckOutcome,
    PrecheckReceiptError,
    build_runtime_precheck_receipt,
)
from composition.precheck_receipt_schema import validate_runtime_precheck_receipt_object
from composition.precheck_receipt_verifier import (
    ReceiptVerificationOutcome,
    verify_runtime_precheck_receipt_payload,
)
from composition.sqlite_inspector import ArtifactFingerprint

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
_CHECKED_AT = "2026-06-16T00:30:00+00:00"


def _fp(name: str, **kwargs: Any) -> ArtifactFingerprint:
    defaults: dict[str, Any] = {
        "present": True,
        "is_regular_file": True,
        "size": 100,
        "sha256": "ab" * 32,
        "user_version": None if name == "execution_inputs_snapshot" else 1,
        "sidecar_suffixes": (),
    }
    defaults.update(kwargs)
    return ArtifactFingerprint(name=name, **defaults)


def _four_fps(**overrides: ArtifactFingerprint) -> tuple[ArtifactFingerprint, ...]:
    names = (
        "execution_inputs_snapshot",
        "ledger",
        "trigger_journal",
        "active_decision_store",
    )
    return tuple(overrides.get(n, _fp(n)) for n in names)


def _receipt_to_dict(receipt: Any) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "checked_at": receipt.checked_at,
        "market": receipt.market,
        "symbol": receipt.symbol,
        "enabled": receipt.enabled,
        "machine_outcome": receipt.machine_outcome,
        "inspection_outcome": receipt.inspection_outcome,
        "reasons": list(receipt.reasons),
        "fingerprints_before": [
            {
                "name": fp.name,
                "present": fp.present,
                "is_regular_file": fp.is_regular_file,
                "size": fp.size,
                "sha256": fp.sha256,
                "user_version": fp.user_version,
                "sidecar_suffixes": list(fp.sidecar_suffixes),
            }
            for fp in receipt.fingerprints_before
        ],
        "fingerprints_after": [
            {
                "name": fp.name,
                "present": fp.present,
                "is_regular_file": fp.is_regular_file,
                "size": fp.size,
                "sha256": fp.sha256,
                "user_version": fp.user_version,
                "sidecar_suffixes": list(fp.sidecar_suffixes),
            }
            for fp in receipt.fingerprints_after
        ],
        "activation_authorized": receipt.activation_authorized,
        "runtime_activation_outcome": receipt.runtime_activation_outcome,
        "explicit_operator_approval_required": receipt.explicit_operator_approval_required,
        "writers_stopped_manual_confirmation_required": (
            receipt.writers_stopped_manual_confirmation_required
        ),
        "receipt_sha256": receipt.receipt_sha256,
    }


def _valid_receipt(**kwargs: Any) -> dict[str, Any]:
    fps = kwargs.pop("fingerprints_before", _four_fps())
    fpa = kwargs.pop("fingerprints_after", fps)
    receipt = build_runtime_precheck_receipt(
        checked_at=kwargs.pop("checked_at", _CHECKED_AT),
        market=kwargs.pop("market", "KR"),
        symbol=kwargs.pop("symbol", "005930"),
        enabled=kwargs.pop("enabled", True),
        machine_outcome=kwargs.pop("machine_outcome", MachineCheckOutcome.PASS),
        inspection_outcome=kwargs.pop("inspection_outcome", InspectionOutcome.OK),
        reasons=kwargs.pop("reasons", ()),
        fingerprints_before=fps,
        fingerprints_after=fpa,
    )
    return _receipt_to_dict(receipt)


# --- builder hardening (H1) ---


@pytest.mark.parametrize(
    ("checked_at", "code"),
    [
        ("not-a-date", "receipt_invalid_checked_at"),
        ("2026-06-16T00:30:00", "receipt_invalid_checked_at"),
    ],
)
def test_builder_rejects_malformed_checked_at(checked_at: str, code: str) -> None:
    fps = _four_fps()
    with pytest.raises(PrecheckReceiptError) as exc:
        build_runtime_precheck_receipt(
            checked_at=checked_at,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=fps,
            fingerprints_after=fps,
        )
    assert exc.value.reason_code == code


def test_builder_rejects_wrong_fingerprint_count() -> None:
    fps = _four_fps()[:3]
    with pytest.raises(PrecheckReceiptError, match="receipt_invalid_fingerprint_count"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=fps,
            fingerprints_after=fps,
        )


def test_builder_rejects_reordered_fingerprints() -> None:
    fps = _four_fps()
    reordered = (fps[1], fps[0], fps[2], fps[3])
    with pytest.raises(PrecheckReceiptError, match="receipt_invalid_fingerprint_order"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=reordered,
            fingerprints_after=reordered,
        )


def test_builder_rejects_duplicate_fingerprint_name() -> None:
    fps = _four_fps()
    dup = (fps[0], fps[0], fps[2], fps[3])
    with pytest.raises(PrecheckReceiptError, match="receipt_invalid_fingerprint_order"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=dup,
            fingerprints_after=dup,
        )


def test_builder_rejects_unknown_fingerprint_name() -> None:
    bad = _fp("unknown_db")
    fps = (bad, _fp("ledger"), _fp("trigger_journal"), _fp("active_decision_store"))
    with pytest.raises(PrecheckReceiptError, match="receipt_invalid_fingerprint_order"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=fps,
            fingerprints_after=fps,
        )


def test_builder_rejects_before_after_name_order_mismatch() -> None:
    before = _four_fps()
    after = (before[0], before[1], before[3], before[2])
    with pytest.raises(PrecheckReceiptError, match="receipt_invalid_fingerprint_order"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=before,
            fingerprints_after=after,
        )


def test_builder_accepts_valid_canonical_input() -> None:
    fps = _four_fps()
    receipt = build_runtime_precheck_receipt(
        checked_at=_CHECKED_AT,
        market="KR",
        symbol="005930",
        enabled=True,
        machine_outcome=MachineCheckOutcome.PASS,
        inspection_outcome=InspectionOutcome.OK,
        reasons=(),
        fingerprints_before=fps,
        fingerprints_after=fps,
    )
    assert receipt.receipt_sha256


# --- verifier valid path ---


def test_verifier_accepts_production_builder_receipt() -> None:
    payload = _valid_receipt()
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.outcome is ReceiptVerificationOutcome.VALID
    assert result.schema_version == 1
    assert result.receipt_sha256 == payload["receipt_sha256"]
    assert result.reason_codes == ()


# --- hash tampering ---


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.update({"checked_at": "2026-06-16T01:00:00+00:00"}),
        lambda p: p.update({"market": "US"}),
        lambda p: p.update({"symbol": "000660"}),
        lambda p: p.update({"reasons": ["missing_database:ledger"]}),
        lambda p: p["fingerprints_before"][1].update({"sha256": "cd" * 32}),
        lambda p: p["fingerprints_before"][1].update({"sidecar_suffixes": ["-wal"]}),
        lambda p: p.update({"activation_authorized": True}),
    ],
)
def test_verifier_rejects_tampered_hash(mutator: Callable[[dict[str, Any]], None]) -> None:
    payload = _valid_receipt()
    mutator(payload)
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.outcome is ReceiptVerificationOutcome.INVALID
    assert result.reason_codes[0] in (
        "receipt_hash_mismatch",
        "receipt_semantic_mismatch",
        "receipt_invalid_activation_posture",
        "receipt_invalid_field",
        "receipt_invalid_market",
        "receipt_invalid_symbol",
    )


def test_verifier_rejects_hash_mismatch_on_checked_at_tamper() -> None:
    payload = _valid_receipt()
    payload["checked_at"] = "2026-06-16T01:00:00+00:00"
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_hash_mismatch",)


# --- strict schema / type ---


def test_verifier_rejects_non_object() -> None:
    result = verify_runtime_precheck_receipt_payload([])
    assert result.reason_codes == ("receipt_not_object",)


def test_verifier_rejects_unknown_top_level_field() -> None:
    payload = _valid_receipt()
    payload["extra"] = 1
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_unknown_field",)


def test_verifier_rejects_missing_top_level_field() -> None:
    payload = _valid_receipt()
    del payload["symbol"]
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_missing_field",)


def test_verifier_rejects_bool_as_int_enabled() -> None:
    payload = _valid_receipt()
    payload["enabled"] = 1
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_field",)


def test_verifier_rejects_unsupported_schema_version() -> None:
    payload = _valid_receipt()
    payload["schema_version"] = 2
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_unsupported_schema",)


def test_verifier_rejects_invalid_timestamp() -> None:
    payload = _valid_receipt()
    payload["checked_at"] = "2026-06-16T00:30:00"
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_checked_at",)


def test_verifier_rejects_invalid_symbol() -> None:
    payload = _valid_receipt()
    payload["symbol"] = "00593"
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_symbol",)


def test_verifier_rejects_invalid_machine_outcome() -> None:
    payload = _valid_receipt()
    payload["machine_outcome"] = "PASS"
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_outcome",)


def test_verifier_rejects_semantic_pass_with_reasons() -> None:
    payload = _valid_receipt()
    payload["reasons"] = ["missing_database:ledger"]
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_semantic_mismatch",)


def test_verifier_rejects_invalid_fingerprint_count() -> None:
    payload = _valid_receipt()
    payload["fingerprints_before"] = payload["fingerprints_before"][:3]
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_fingerprint_count",)


def test_verifier_rejects_duplicate_sidecar_suffix() -> None:
    payload = _valid_receipt()
    payload["fingerprints_before"][1]["sidecar_suffixes"] = ["-wal", "-wal"]
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_fingerprint",)


def test_verifier_rejects_out_of_order_sidecar_suffixes() -> None:
    payload = _valid_receipt()
    payload["fingerprints_before"][1]["sidecar_suffixes"] = ["-journal", "-wal"]
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_fingerprint",)


def test_verifier_rejects_invalid_hash_format() -> None:
    payload = _valid_receipt()
    payload["receipt_sha256"] = "ZZ" * 32
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_field",)


def test_verifier_sanitized_invalid_payload_output() -> None:
    poison = _valid_receipt()
    poison["symbol"] = "/home/user/KIS_LIVE_APP_KEY/secret"
    poison["reasons"] = ["traceback OperationalError sqlite3"]
    result = verify_runtime_precheck_receipt_payload(poison)
    serialized = json.dumps(
        {
            "outcome": result.outcome.value,
            "reason_codes": list(result.reason_codes),
            "schema_version": result.schema_version,
            "receipt_sha256": result.receipt_sha256,
        }
    )
    assert "/home/" not in serialized
    assert _REPO_ROOT not in serialized
    assert "KIS_" not in serialized
    assert "APP_KEY" not in serialized
    assert "Traceback" not in serialized
    assert "OperationalError" not in serialized


# --- builder ↔ verifier parity (RTM-7c.4e closure) ---


def test_builder_success_implies_verifier_valid() -> None:
    fps = _four_fps()
    receipt = build_runtime_precheck_receipt(
        checked_at=_CHECKED_AT,
        market="KR",
        symbol="005930",
        enabled=True,
        machine_outcome=MachineCheckOutcome.PASS,
        inspection_outcome=InspectionOutcome.OK,
        reasons=(),
        fingerprints_before=fps,
        fingerprints_after=fps,
    )
    result = verify_runtime_precheck_receipt_payload(_receipt_to_dict(receipt))
    assert result.outcome is ReceiptVerificationOutcome.VALID
    sha = validate_runtime_precheck_receipt_object(
        schema_version=receipt.schema_version,
        checked_at=receipt.checked_at,
        market=receipt.market,
        symbol=receipt.symbol,
        enabled=receipt.enabled,
        machine_outcome=receipt.machine_outcome,
        inspection_outcome=receipt.inspection_outcome,
        reasons=receipt.reasons,
        fingerprints_before=receipt.fingerprints_before,
        fingerprints_after=receipt.fingerprints_after,
        activation_authorized=receipt.activation_authorized,
        runtime_activation_outcome=receipt.runtime_activation_outcome,
        explicit_operator_approval_required=receipt.explicit_operator_approval_required,
        writers_stopped_manual_confirmation_required=(
            receipt.writers_stopped_manual_confirmation_required
        ),
        receipt_sha256=receipt.receipt_sha256,
    )
    assert sha == receipt.receipt_sha256


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"market": "US"}, "receipt_invalid_market"),
        ({"symbol": "00593"}, "receipt_invalid_symbol"),
        (
            {
                "machine_outcome": MachineCheckOutcome.PASS,
                "reasons": ("missing_database:ledger",),
            },
            "receipt_semantic_mismatch",
        ),
        (
            {
                "machine_outcome": MachineCheckOutcome.PASS,
                "inspection_outcome": InspectionOutcome.NO_GO,
            },
            "receipt_semantic_mismatch",
        ),
        (
            {
                "machine_outcome": MachineCheckOutcome.NO_GO,
                "inspection_outcome": InspectionOutcome.NO_GO,
                "reasons": (),
            },
            "receipt_semantic_mismatch",
        ),
    ],
)
def test_builder_rejects_verifier_invalid_inputs(kwargs: dict[str, Any], code: str) -> None:
    fps = _four_fps()
    base = {
        "checked_at": _CHECKED_AT,
        "market": "KR",
        "symbol": "005930",
        "enabled": True,
        "machine_outcome": MachineCheckOutcome.PASS,
        "inspection_outcome": InspectionOutcome.OK,
        "reasons": (),
        "fingerprints_before": fps,
        "fingerprints_after": fps,
    }
    base.update(kwargs)
    with pytest.raises(PrecheckReceiptError) as exc:
        build_runtime_precheck_receipt(**base)
    assert exc.value.reason_code == code


@pytest.mark.parametrize(
    ("fp_kwargs",),
    [
        ({"present": 1},),
        ({"is_regular_file": 1},),
        ({"size": 1.5},),
        ({"size": True},),
        ({"present": False, "is_regular_file": True, "size": 1},),
        ({"present": False, "is_regular_file": False, "sha256": "ab" * 32},),
        ({"present": False, "is_regular_file": False, "sidecar_suffixes": ("-wal",)},),
        ({"present": False, "is_regular_file": False, "sidecar_suffixes": ("-invalid",)},),
        ({"present": False, "is_regular_file": False, "sidecar_suffixes": ("-wal", "-wal")},),
        ({"present": True, "is_regular_file": False, "size": 10},),
        ({"present": True, "is_regular_file": True, "sha256": None},),
        ({"present": True, "is_regular_file": True, "sha256": "AB" * 32},),
        ({"name": "execution_inputs_snapshot", "user_version": 1},),
        ({"user_version": -1},),
        ({"sidecar_suffixes": ("-bad",)},),
        ({"sidecar_suffixes": ("-wal", "-wal")},),
    ],
)
def test_builder_rejects_invalid_fingerprint_semantics(fp_kwargs: dict[str, Any]) -> None:
    name = fp_kwargs.pop("name", "ledger")
    bad = _fp(name, **fp_kwargs)
    fps = _four_fps(**{name: bad})
    with pytest.raises(PrecheckReceiptError) as exc:
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=fps,
            fingerprints_after=fps,
        )
    assert exc.value.reason_code in (
        "receipt_invalid_fingerprint",
        "receipt_invalid_fingerprint_order",
    )
    assert str(exc.value) == exc.value.reason_code


def _absent_fp(name: str) -> ArtifactFingerprint:
    return ArtifactFingerprint(
        name=name,
        present=False,
        is_regular_file=False,
        size=None,
        sha256=None,
        user_version=None,
        sidecar_suffixes=(),
    )


def _irregular_fp(name: str, *, sidecars: tuple[str, ...] = ()) -> ArtifactFingerprint:
    return ArtifactFingerprint(
        name=name,
        present=True,
        is_regular_file=False,
        size=None,
        sha256=None,
        user_version=None,
        sidecar_suffixes=sidecars,
    )


@pytest.mark.parametrize(
    "fps_factory",
    [
        lambda: _four_fps(),
        lambda: (
            _absent_fp("execution_inputs_snapshot"),
            _absent_fp("ledger"),
            _absent_fp("trigger_journal"),
            _absent_fp("active_decision_store"),
        ),
        lambda: (
            _irregular_fp("execution_inputs_snapshot"),
            _irregular_fp("ledger"),
            _irregular_fp("trigger_journal"),
            _irregular_fp("active_decision_store"),
        ),
        lambda: _four_fps(
            ledger=_fp("ledger", user_version=1),
            trigger_journal=_fp("trigger_journal", user_version=1),
            active_decision_store=_fp("active_decision_store", user_version=1),
        ),
        lambda: _four_fps(
            ledger=_fp("ledger", user_version=None),
            trigger_journal=_fp("trigger_journal", user_version=None),
            active_decision_store=_fp("active_decision_store", user_version=None),
        ),
    ],
    ids=["regular-json", "absent-canonical", "irregular-canonical", "sqlite-user-version", "sqlite-null-user-version"],
)
def test_builder_success_implies_verifier_valid_matrix(fps_factory: Callable[[], tuple[ArtifactFingerprint, ...]]) -> None:
    fps = fps_factory()
    receipt = build_runtime_precheck_receipt(
        checked_at=_CHECKED_AT,
        market="KR",
        symbol="005930",
        enabled=True,
        machine_outcome=MachineCheckOutcome.PASS,
        inspection_outcome=InspectionOutcome.OK,
        reasons=(),
        fingerprints_before=fps,
        fingerprints_after=fps,
    )
    result = verify_runtime_precheck_receipt_payload(_receipt_to_dict(receipt))
    assert result.outcome is ReceiptVerificationOutcome.VALID


# --- ASCII-only KRX symbol ---


@pytest.mark.parametrize(
    ("symbol", "valid"),
    [
        ("005930", True),
        ("１２３４５６", False),
        ("١٢٣٤٥٦", False),
        ("00593０", False),
    ],
)
def test_symbol_ascii_digits_only(symbol: str, valid: bool) -> None:
    from composition.precheck_receipt_schema import symbol_valid, validate_symbol

    assert symbol_valid(symbol) is valid
    if valid:
        validate_symbol(symbol)
    else:
        with pytest.raises(PrecheckReceiptError, match="receipt_invalid_symbol"):
            validate_symbol(symbol)


def test_builder_rejects_unicode_symbol() -> None:
    fps = _four_fps()
    with pytest.raises(PrecheckReceiptError, match="receipt_invalid_symbol"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="１２３４５６",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=fps,
            fingerprints_after=fps,
        )


def test_verifier_rejects_unicode_symbol() -> None:
    payload = _valid_receipt()
    payload["symbol"] = "１２３４５６"
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_invalid_symbol",)


# --- RTM-7c.4f observation semantics closure ---


def _after_with_ledger_drift(before: tuple[ArtifactFingerprint, ...]) -> tuple[ArtifactFingerprint, ...]:
    return tuple(
        _fp("ledger", sha256="cd" * 32) if fp.name == "ledger" else fp for fp in before
    )


def test_pass_receipt_fingerprint_drift_rejected_by_builder_and_verifier() -> None:
    """P1 재현 — PASS + ledger SHA drift는 builder·verifier 모두 거부."""
    before = _four_fps()
    after = _after_with_ledger_drift(before)
    with pytest.raises(PrecheckReceiptError) as exc:
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=before,
            fingerprints_after=after,
        )
    assert exc.value.reason_code == "receipt_semantic_mismatch"
    assert str(exc.value) == "receipt_semantic_mismatch"


@pytest.mark.parametrize(
    ("after_override",),
    [
        (_fp("ledger", sidecar_suffixes=("-wal",)),),
        (_fp("ledger", size=8192),),
        (_fp("ledger", user_version=2),),
    ],
    ids=["sidecar", "size", "user_version"],
)
def test_builder_rejects_pass_with_fingerprint_field_drift(
    after_override: ArtifactFingerprint,
) -> None:
    before = _four_fps()
    after = tuple(after_override if fp.name == "ledger" else fp for fp in before)
    with pytest.raises(PrecheckReceiptError, match="receipt_semantic_mismatch"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=before,
            fingerprints_after=after,
        )


@pytest.mark.parametrize(
    ("reasons", "after_factory"),
    [
        (("missing_database:ledger",), _after_with_ledger_drift),
        (("precheck_artifact_changed:ledger",), lambda b: b),
        (
            ("precheck_artifact_changed:ledger", "precheck_artifact_changed:ledger"),
            _after_with_ledger_drift,
        ),
        (("precheck_artifact_changed:unknown",), _after_with_ledger_drift),
    ],
    ids=["missing_changed_reason", "spurious_changed_reason", "duplicate_changed_reason", "unknown_artifact"],
)
def test_builder_rejects_no_go_drift_reason_inconsistency(
    reasons: tuple[str, ...],
    after_factory: Callable[[tuple[ArtifactFingerprint, ...]], tuple[ArtifactFingerprint, ...]],
) -> None:
    before = _four_fps()
    after = after_factory(before)
    with pytest.raises(PrecheckReceiptError, match="receipt_semantic_mismatch"):
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.NO_GO,
            inspection_outcome=InspectionOutcome.NO_GO,
            reasons=reasons,
            fingerprints_before=before,
            fingerprints_after=after,
        )


def test_builder_accepts_no_go_non_drift_reason() -> None:
    fps = _four_fps()
    receipt = build_runtime_precheck_receipt(
        checked_at=_CHECKED_AT,
        market="KR",
        symbol="005930",
        enabled=True,
        machine_outcome=MachineCheckOutcome.NO_GO,
        inspection_outcome=InspectionOutcome.NO_GO,
        reasons=("missing_database:ledger",),
        fingerprints_before=fps,
        fingerprints_after=fps,
    )
    result = verify_runtime_precheck_receipt_payload(_receipt_to_dict(receipt))
    assert result.outcome is ReceiptVerificationOutcome.VALID


def test_builder_accepts_no_go_single_artifact_drift() -> None:
    before = _four_fps()
    after = _after_with_ledger_drift(before)
    receipt = build_runtime_precheck_receipt(
        checked_at=_CHECKED_AT,
        market="KR",
        symbol="005930",
        enabled=True,
        machine_outcome=MachineCheckOutcome.NO_GO,
        inspection_outcome=InspectionOutcome.OK,
        reasons=("precheck_artifact_changed:ledger",),
        fingerprints_before=before,
        fingerprints_after=after,
    )
    result = verify_runtime_precheck_receipt_payload(_receipt_to_dict(receipt))
    assert result.outcome is ReceiptVerificationOutcome.VALID


def test_builder_accepts_no_go_multiple_artifact_drift() -> None:
    before = _four_fps()
    after = (
        before[0],
        _fp("ledger", sha256="cd" * 32),
        before[2],
        _fp("active_decision_store", sha256="ef" * 32),
    )
    receipt = build_runtime_precheck_receipt(
        checked_at=_CHECKED_AT,
        market="KR",
        symbol="005930",
        enabled=True,
        machine_outcome=MachineCheckOutcome.NO_GO,
        inspection_outcome=InspectionOutcome.OK,
        reasons=(
            "precheck_artifact_changed:ledger",
            "precheck_artifact_changed:active_decision_store",
        ),
        fingerprints_before=before,
        fingerprints_after=after,
    )
    result = verify_runtime_precheck_receipt_payload(_receipt_to_dict(receipt))
    assert result.outcome is ReceiptVerificationOutcome.VALID


def test_verifier_semantic_mismatch_precedes_hash_mismatch_on_pass_drift() -> None:
    payload = _valid_receipt()
    payload["fingerprints_after"][1]["sha256"] = "cd" * 32
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_semantic_mismatch",)


def test_verifier_rejects_pass_drift_even_with_recomputed_hash() -> None:
    before = _four_fps()
    after = _after_with_ledger_drift(before)
    no_go_payload = _receipt_to_dict(
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.NO_GO,
            inspection_outcome=InspectionOutcome.OK,
            reasons=("precheck_artifact_changed:ledger",),
            fingerprints_before=before,
            fingerprints_after=after,
        )
    )
    payload = dict(no_go_payload)
    payload["machine_outcome"] = "pass"
    payload["inspection_outcome"] = "ok"
    payload["reasons"] = []
    from composition.precheck_receipt_schema import (
        build_receipt_hash_payload_from_fingerprint_dicts,
        compute_receipt_sha256,
    )

    hash_payload = build_receipt_hash_payload_from_fingerprint_dicts(
        schema_version=payload["schema_version"],
        checked_at=payload["checked_at"],
        market=payload["market"],
        symbol=payload["symbol"],
        enabled=payload["enabled"],
        machine_outcome="pass",
        inspection_outcome="ok",
        reasons=[],
        fingerprints_before=payload["fingerprints_before"],
        fingerprints_after=payload["fingerprints_after"],
    )
    payload["receipt_sha256"] = compute_receipt_sha256(hash_payload)
    result = verify_runtime_precheck_receipt_payload(payload)
    assert result.reason_codes == ("receipt_semantic_mismatch",)


def test_semantic_mismatch_error_is_sanitized() -> None:
    before = _four_fps()
    after = _after_with_ledger_drift(before)
    try:
        build_runtime_precheck_receipt(
            checked_at=_CHECKED_AT,
            market="KR",
            symbol="005930",
            enabled=True,
            machine_outcome=MachineCheckOutcome.PASS,
            inspection_outcome=InspectionOutcome.OK,
            reasons=(),
            fingerprints_before=before,
            fingerprints_after=after,
        )
    except PrecheckReceiptError as exc:
        assert exc.reason_code == "receipt_semantic_mismatch"
        assert "cd" not in str(exc)
        assert "ledger" not in str(exc)
        assert "ab" not in str(exc)
    else:
        pytest.fail("expected PrecheckReceiptError")
