"""RTM-7c.4j — immutable verified precheck receipt snapshot tests.

Covers: VALID build produces a fully immutable observation (frozen dataclass, frozen
``ArtifactFingerprint`` tuples); INVALID / non-object payloads fail closed to the single
``receipt_snapshot_invalid`` reason; a post-build mutation of the raw payload dict (and its
nested lists) cannot change the snapshot or its hash; ``checked_at`` is aware and
``checked_at_iso`` is the exact verified source string bound into the receipt hash;
activation posture is the canonical NO-GO; atomic verify-and-snapshot uses a detached copy
so verify-return and nested mutations cannot mix hash vs field observations; and the module
reads no clock of its own.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from composition.precheck_receipt_verifier import (
    ReceiptVerificationOutcome,
    verify_runtime_precheck_receipt_payload,
)
from composition.sqlite_inspector import ArtifactFingerprint
from composition.verified_precheck_receipt import (
    VerifiedPrecheckReceipt,
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)

import test_precheck_receipt_verifier as vrf_helper

_CHECKED_AT = vrf_helper._CHECKED_AT
_MUTATED_CHECKED_AT = "2026-06-16T01:00:00+00:00"
_MUTATED_SYMBOL = "000660"


def _snapshot(payload: object) -> VerifiedPrecheckReceipt:
    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.VALID
    assert result.reasons == ()
    assert result.receipt is not None
    return result.receipt


# --- VALID build: immutable observation ---


def test_valid_payload_builds_immutable_snapshot() -> None:
    payload = vrf_helper._valid_receipt()
    receipt = _snapshot(payload)

    assert receipt.receipt_sha256 == payload["receipt_sha256"]
    assert receipt.market == "KR"
    assert receipt.symbol == "005930"
    assert receipt.enabled is True
    assert receipt.machine_outcome == "pass"
    assert receipt.inspection_outcome == "ok"
    assert receipt.reasons == ()
    assert isinstance(receipt.fingerprints_before, tuple)
    assert isinstance(receipt.fingerprints_after, tuple)
    assert all(isinstance(fp, ArtifactFingerprint) for fp in receipt.fingerprints_after)
    assert all(isinstance(fp.sidecar_suffixes, tuple) for fp in receipt.fingerprints_after)


def test_snapshot_checked_at_is_aware_and_iso_is_original() -> None:
    payload = vrf_helper._valid_receipt()
    receipt = _snapshot(payload)

    assert isinstance(receipt.checked_at, datetime)
    assert receipt.checked_at.tzinfo is not None
    assert receipt.checked_at.utcoffset() is not None
    assert receipt.checked_at_iso == _CHECKED_AT
    assert receipt.checked_at_iso == payload["checked_at"]


def test_snapshot_carries_canonical_no_go_activation_posture() -> None:
    receipt = _snapshot(vrf_helper._valid_receipt())

    assert receipt.activation_authorized is False
    assert receipt.runtime_activation_outcome == "no_go"
    assert receipt.explicit_operator_approval_required is True
    assert receipt.writers_stopped_manual_confirmation_required is True


def test_snapshot_dataclass_is_frozen() -> None:
    receipt = _snapshot(vrf_helper._valid_receipt())
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.market = "US"  # type: ignore[misc]


# --- mutation isolation ---


def test_raw_payload_mutation_after_build_does_not_change_snapshot() -> None:
    payload = vrf_helper._valid_receipt()
    receipt = _snapshot(payload)
    original_hash = receipt.receipt_sha256
    original_before = receipt.fingerprints_before
    original_after = receipt.fingerprints_after

    payload["receipt_sha256"] = "ff" * 32
    payload["checked_at"] = "2099-01-01T00:00:00+00:00"
    payload["market"] = "US"
    payload["symbol"] = "TAMPERED"
    payload["reasons"] = ["tampered"]

    assert receipt.receipt_sha256 == original_hash
    assert receipt.checked_at_iso == _CHECKED_AT
    assert receipt.market == "KR"
    assert receipt.symbol == "005930"
    assert receipt.reasons == ()
    assert receipt.fingerprints_before == original_before
    assert receipt.fingerprints_after == original_after


def test_nested_collection_mutation_after_build_does_not_change_snapshot() -> None:
    payload = vrf_helper._valid_receipt()
    receipt = _snapshot(payload)
    original_after = receipt.fingerprints_after

    # mutate the nested lists the snapshot was built from
    payload["fingerprints_after"][0]["sidecar_suffixes"].append("-tampered")
    payload["fingerprints_after"].clear()
    if isinstance(payload["reasons"], list):
        payload["reasons"].append("tampered")

    assert receipt.fingerprints_after == original_after
    assert receipt.reasons == ()
    assert all(isinstance(fp.sidecar_suffixes, tuple) for fp in receipt.fingerprints_after)


# --- fail-closed paths ---


def test_invalid_payload_fails_closed() -> None:
    payload = vrf_helper._valid_receipt()
    payload["receipt_sha256"] = "00" * 32  # hash mismatch → verifier INVALID

    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.INVALID
    assert result.reasons == ("receipt_snapshot_invalid",)
    assert result.receipt is None


@pytest.mark.parametrize("payload", [None, 42, "receipt", ["not", "a", "dict"], object()])
def test_non_dict_payload_fails_closed(payload: object) -> None:
    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.INVALID
    assert result.reasons == ("receipt_snapshot_invalid",)
    assert result.receipt is None


# --- no clock read ---


def test_module_source_reads_no_clock() -> None:
    source = Path("src/composition/verified_precheck_receipt.py").read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "datetime.utcnow", "time.time", "time.monotonic"):
        assert forbidden not in source


# --- RTM-7c.4j atomic snapshot closure: verify/copy TOCTOU + detached tree ---


def _mutated_fingerprints_after(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a valid fingerprints_after list that differs from the receipt's current one."""

    fps = copy.deepcopy(payload["fingerprints_after"])
    fps[0] = dict(fps[0])
    fps[0]["sha256"] = "cd" * 32
    return fps


def test_mutation_during_verification_return_does_not_mix_hash_and_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier VALID 후 caller payload 변경이 snapshot hash/field observation을 섞지 않는다.

    Detached copy 없이는 verification hash(A)와 mutation 후 field read(B)가 혼합될 수 있다.
    Atomic closure 후 snapshot은 mutation 전 detached observation만 반영해야 한다."""

    payload = vrf_helper._valid_receipt()
    original_hash = payload["receipt_sha256"]
    original_checked_at = payload["checked_at"]
    original_symbol = payload["symbol"]
    original_after_sha = payload["fingerprints_after"][0]["sha256"]
    real_verifier = verify_runtime_precheck_receipt_payload

    def _mutate_on_valid_verifier(detached: object) -> Any:
        result = real_verifier(detached)
        if result.outcome is ReceiptVerificationOutcome.VALID:
            payload["checked_at"] = _MUTATED_CHECKED_AT
            payload["symbol"] = _MUTATED_SYMBOL
            payload["fingerprints_after"] = _mutated_fingerprints_after(payload)
        return result

    monkeypatch.setattr(
        "composition.verified_precheck_receipt.verify_runtime_precheck_receipt_payload",
        _mutate_on_valid_verifier,
    )

    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.VALID
    assert result.receipt is not None
    receipt = result.receipt

    assert receipt.receipt_sha256 == original_hash
    assert receipt.checked_at_iso == original_checked_at
    assert receipt.symbol == original_symbol
    assert receipt.fingerprints_after[0].sha256 == original_after_sha
    assert payload["checked_at"] == _MUTATED_CHECKED_AT
    assert payload["symbol"] == _MUTATED_SYMBOL


def test_verifier_and_snapshot_extraction_share_detached_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier argument와 snapshot extraction argument는 동일 detached tree여야 한다."""

    payload = vrf_helper._valid_receipt()
    caller_id = id(payload)
    verifier_payload_ids: list[int] = []
    snapshot_payload_ids: list[int] = []
    real_verifier = verify_runtime_precheck_receipt_payload
    import composition.verified_precheck_receipt as snap_mod

    real_snapshot = snap_mod._snapshot_from_verified_payload

    def _spy_verifier(arg: object) -> Any:
        verifier_payload_ids.append(id(arg))
        assert isinstance(arg, dict)
        assert id(arg["fingerprints_after"]) != id(payload["fingerprints_after"])
        assert id(arg["reasons"]) != id(payload["reasons"])
        return real_verifier(arg)

    def _spy_snapshot(**kwargs: Any) -> Any:
        snap_payload = kwargs["payload"]
        snapshot_payload_ids.append(id(snap_payload))
        return real_snapshot(**kwargs)

    monkeypatch.setattr(snap_mod, "verify_runtime_precheck_receipt_payload", _spy_verifier)
    monkeypatch.setattr(snap_mod, "_snapshot_from_verified_payload", _spy_snapshot)

    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.VALID
    assert len(verifier_payload_ids) == 1
    assert len(snapshot_payload_ids) == 1
    assert verifier_payload_ids[0] == snapshot_payload_ids[0]
    assert verifier_payload_ids[0] != caller_id


def test_verify_and_snapshot_calls_verifier_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = vrf_helper._valid_receipt()
    calls: list[int] = []
    real = verify_runtime_precheck_receipt_payload

    def _spy(arg: object) -> Any:
        calls.append(1)
        return real(arg)

    monkeypatch.setattr(
        "composition.verified_precheck_receipt.verify_runtime_precheck_receipt_payload",
        _spy,
    )
    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.VALID
    assert len(calls) == 1


def test_nested_mutation_during_verify_does_not_change_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier 수행 중 caller nested list mutation이 snapshot에 반영되지 않는다."""

    payload = vrf_helper._valid_receipt()
    expected_after_sha = payload["fingerprints_after"][0]["sha256"]
    real_verifier = verify_runtime_precheck_receipt_payload

    def _mutate_nested_on_valid(detached: object) -> Any:
        result = real_verifier(detached)
        if result.outcome is ReceiptVerificationOutcome.VALID:
            payload["reasons"].append("tampered-reason")
            payload["fingerprints_after"].clear()
            payload["fingerprints_after"].append(
                {
                    "name": "ledger",
                    "present": True,
                    "is_regular_file": True,
                    "size": 1,
                    "sha256": "ee" * 32,
                    "user_version": 1,
                    "sidecar_suffixes": ["-wal"],
                }
            )
            if payload["fingerprints_before"]:
                payload["fingerprints_before"][0]["sidecar_suffixes"].append("-tampered")
        return result

    monkeypatch.setattr(
        "composition.verified_precheck_receipt.verify_runtime_precheck_receipt_payload",
        _mutate_nested_on_valid,
    )

    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.VALID
    assert result.receipt is not None
    receipt = result.receipt

    assert receipt.reasons == ()
    assert receipt.fingerprints_after[0].sha256 == expected_after_sha
    assert len(receipt.fingerprints_after) == 4
    assert receipt.fingerprints_before[0].sidecar_suffixes == ()


class _PoisonDeepcopyDict(dict):
    """``copy.deepcopy`` 중 예외를 유발하는 sentinel — stdout/stderr leak 테스트용."""

    _SENTINEL = "sentinel_poison_deepcopy_leak_test"

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        raise RuntimeError(self._SENTINEL)


def test_clone_failure_fails_closed_without_leak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    poison = _PoisonDeepcopyDict(vrf_helper._valid_receipt())

    result = verify_and_snapshot_precheck_receipt(poison)
    captured = capsys.readouterr()

    assert result.outcome is VerifiedReceiptSnapshotOutcome.INVALID
    assert result.reasons == ("receipt_snapshot_invalid",)
    assert result.receipt is None
    assert _PoisonDeepcopyDict._SENTINEL not in captured.out
    assert _PoisonDeepcopyDict._SENTINEL not in captured.err
    assert "RuntimeError" not in captured.out
    assert "RuntimeError" not in captured.err
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_non_dict_payload_never_reaches_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _fail_verifier(_: object) -> object:
        calls.append(1)
        raise AssertionError("verifier must not run for non-dict payload")

    monkeypatch.setattr(
        "composition.verified_precheck_receipt.verify_runtime_precheck_receipt_payload",
        _fail_verifier,
    )
    for payload in (None, 42, "receipt", ["not", "a", "dict"], object()):
        result = verify_and_snapshot_precheck_receipt(payload)
        assert result.outcome is VerifiedReceiptSnapshotOutcome.INVALID
        assert result.reasons == ("receipt_snapshot_invalid",)
    assert calls == []
