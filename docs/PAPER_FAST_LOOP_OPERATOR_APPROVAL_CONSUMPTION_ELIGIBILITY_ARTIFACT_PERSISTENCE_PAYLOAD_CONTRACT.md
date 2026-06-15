# Operator Approval Consumption Eligibility Artifact Persistence Payload Contract (RTM-7c.4w)

Canonical **byte format** and strict round-trip for a verified Operator approval-consumption
eligibility artifact snapshot (RTM-7c.4u output). API-only — this lane fixes the persistence
*payload bytes* and the decode/verify path **before** any actual file writer/reader exists.

**Runtime activation: NO-GO.** A decode `VALID` verdict is an artifact schema·semantic·hash
**consistency** observation only — never actual file persistence, payload origin/provenance,
Operator identity, signature/HMAC, approval consumption, replay prevention, or activation
authorization.

**No file I/O in this lane.** Neither function creates, reads, opens, or names any file or path.
"Payload created" is **not** "payload persisted". The actual atomic writer/reader CLI is a
follow-on lane designed only after this format passes independent review.

Code:

- `composition.operator_approval_consumption_eligibility_artifact_persistence_payload.encode_verified_operator_approval_consumption_eligibility_artifact`
- `composition.operator_approval_consumption_eligibility_artifact_persistence_payload.decode_operator_approval_consumption_eligibility_artifact_payload`
- reuses `decision.canonical_json.canonical_json_dumps` (encode), the bounded strict JSON parser
  `composition.precheck_receipt_stdin_json.parse_receipt_stdin_json` (decode), and the existing
  artifact verifier / `verify_and_snapshot_...` (both)

## Canonical payload format

The persistence payload **is** the existing 13-field artifact JSON object — no new wrapper or
envelope is nested, and no separate persistence hash is created (the artifact's own
`eligibility_artifact_sha256` is the only digest). Exact field set:

```
schema_version, checked_at, approval_intent_schema_version, approval_intent_sha256,
candidate_evidence_schema_version, candidate_evidence_sha256, market, symbol,
evidence_evaluated_at, intent_declared_at, activation_authorized,
runtime_activation_outcome, eligibility_artifact_sha256
```

Encoding: `canonical_json_dumps` (sorted keys, `(",", ":")` separators) → UTF-8, **no trailing
newline, no BOM, no indentation**. An identical snapshot always produces byte-for-byte identical
payload bytes.

## Encoder contract

`encode(snapshot) -> EligibilityArtifactPersistencePayloadResult`

```
outcome: CREATED | INVALID
reason_codes: tuple[str, ...]
payload_bytes: bytes | None
eligibility_artifact_sha256: str | None
```

- `CREATED`: `reason_codes == ()`, `payload_bytes` is `bytes`, `eligibility_artifact_sha256` exact
  lowercase hex64.
- `INVALID`: `len(reason_codes) == 1`, `payload_bytes is None`, `eligibility_artifact_sha256 is None`.
- Stable reason: `eligibility_persistence_payload_invalid_snapshot`.

Processing: exact `type(snapshot) is VerifiedOperatorApprovalConsumptionEligibilityArtifact`
(subclass / `object.__setattr__`-corrupted / arbitrary object rejected — `asdict` is not trusted);
each of the 13 scalars read once into a built-in dict; re-validated through the existing artifact
verifier **exactly once**; `canonical_json_dumps` + UTF-8 only on VALID. The caller snapshot is
never re-accessed after capture, so post-call mutation cannot change the result.

## Decoder contract

`decode(payload_bytes) -> EligibilityArtifactPersistencePayloadVerification`

```
outcome: VALID | INVALID
reason_codes: tuple[str, ...]
snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact | None
```

- `VALID`: `reason_codes == ()`, `snapshot is not None`.
- `INVALID`: `len(reason_codes) == 1`, `snapshot is None`.

Input reasons:

```
eligibility_persistence_payload_not_bytes   (bytearray/memoryview/bytes-subclass/str/...)
eligibility_persistence_payload_empty
eligibility_persistence_payload_too_large   (> 1 MiB)
eligibility_persistence_payload_not_utf8
eligibility_persistence_payload_not_json    (incl. NaN / Infinity / -Infinity)
eligibility_persistence_payload_too_deep
eligibility_persistence_payload_duplicate_key  (top-level or nested)
```

When the artifact verifier rejects a syntactically valid payload, its reason is preserved verbatim
(e.g. `eligibility_artifact_not_object` for a root list/string/null, `eligibility_artifact_missing_field`,
`eligibility_artifact_invalid_field`, `eligibility_artifact_hash_mismatch`).

Processing: exact `type(payload_bytes) is bytes`; empty / 1 MiB bound; UTF-8 decode; bounded strict
JSON parse; `verify_and_snapshot_...` **exactly once**. Parser and verifier responsibilities are
not mixed — the parser only requires syntactically valid bounded JSON; the verifier owns the exact
object-root and 13-field requirement.

## Consistency semantics (A / B / C)

Decode `VALID` means **only** that the canonical payload satisfies artifact schema/semantic/hash
consistency. It does **not** mean the payload was persisted, nor anything about origin/provenance,
Operator identity, signature, consumption, replay, or activation.

- **A** — `builder → verify_and_snapshot → encode → decode` round-trips `VALID`; the decoded 13
  fields equal the original and `encode(original) == encode(decoded)`.
- **B** — a freshly minted, semantically valid payload (e.g. a different symbol) with a **correctly
  recomputed** digest decodes `VALID` **by design**. This is observational consistency, not proof
  of provenance/authenticity.
- **C** — a content change with a **stale** digest decodes `INVALID` / `eligibility_artifact_hash_mismatch`.

## What decode VALID does **not** mean

- Actual file persistence (payload created ≠ persisted; no file/path/DB touched)
- Payload origin / provenance
- Operator identity authentication
- Signature / HMAC
- Approval consumption / consumed marker
- Replay / nonce / idempotency prevention
- Activation authorization (`activation_authorized` stays `false`; `runtime_activation_outcome`
  stays `"no_go"`)

## Isolation, call counts, exceptions

- Encoder per call: artifact verify `1`, `canonical_json_dumps` `1`; filesystem/path/clock/
  config/env/DB/network `0`; builder/eligibility/intent-verifier rerun `0`.
- Decoder per call: UTF-8 decode `1`, JSON parse `1`, verify-and-snapshot `1`; filesystem/path/
  clock/config/env/DB/network `0`.
- Decoder retains no raw `payload_bytes` reference and no parser raw dict in the result/snapshot.
- Ordinary `Exception` → sanitized `INVALID` (no raw field / hash / timestamp / path / exception);
  `MemoryError` / `KeyboardInterrupt` / `SystemExit` re-raised. Applies to both functions.

## Out of scope (deferred)

Actual file persistence / atomic writer / reader CLI / external-file input; actual approval
consumption; consumed marker; replay/nonce/idempotency; signing/HMAC; Operator identity
authentication; provenance verification; intent/evidence lookup; TTL/freshness re-evaluation;
activation caller/token; `--run`; KIS/network; broker/order; operational DB write; daemon/scheduler.
