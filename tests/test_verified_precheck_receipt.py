"""RTM-7c.4j — immutable verified precheck receipt snapshot tests.

Covers: VALID build produces a fully immutable observation (frozen dataclass, frozen
``ArtifactFingerprint`` tuples); INVALID / non-object payloads fail closed to the single
``receipt_snapshot_invalid`` reason; a post-build mutation of the raw payload dict (and its
nested lists) cannot change the snapshot or its hash; ``checked_at`` is aware and
``checked_at_iso`` is byte-identical to the original; activation posture is the canonical
NO-GO; and the module reads no clock of its own.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from composition.sqlite_inspector import ArtifactFingerprint
from composition.verified_precheck_receipt import (
    VerifiedPrecheckReceipt,
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)

import test_precheck_receipt_verifier as vrf_helper

_CHECKED_AT = vrf_helper._CHECKED_AT


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
