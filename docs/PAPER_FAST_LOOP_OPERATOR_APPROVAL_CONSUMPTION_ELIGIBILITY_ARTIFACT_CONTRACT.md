# Operator Approval Consumption Eligibility Artifact Contract (RTM-7c.4t)

Pure builder API that freezes an already-produced **ELIGIBLE** consumption-eligibility
observation (RTM-7c.4s) into a canonical immutable artifact with a stable digest.

**This is not approval consumption.** The artifact is a canonical eligibility *observation* —
no consumed marker, replay/nonce/idempotency protection, persistence, signing/HMAC, Operator
identity authentication, intent TTL, evidence freshness re-evaluation, or runtime activation
authorization.

**Runtime activation: NO-GO** on every created artifact (`activation_authorized=false`,
`runtime_activation_outcome="no_go"`).

The upstream eligibility API, intent verifier, and evidence validator are **not** re-run — the
builder reads the completed 4s result only. No clock read.

Code: `composition.operator_approval_consumption_eligibility_artifact.build_operator_approval_consumption_eligibility_artifact`

## What the artifact means

> A completed 4s `ELIGIBLE` observation has been frozen, field-revalidated for strict shape and
> time ordering, and bound to a deterministic canonical digest over its 12 content fields.

## What the artifact does **not** mean

- Actual approval consumption
- Consumed / replayed state, nonce / idempotency
- Signing / HMAC / Operator identity authentication
- Intent TTL / evidence freshness re-evaluation
- Persistence (no file/DB write — the artifact is returned in-memory only)
- Activation authorization

## API

```python
build_operator_approval_consumption_eligibility_artifact(
    eligibility_result: object,
) -> OperatorApprovalConsumptionEligibilityArtifactResult
```

## Outcome model

```python
class OperatorApprovalConsumptionEligibilityArtifactOutcome(StrEnum):
    CREATED = "created"
    NOT_ELIGIBLE = "not_eligible"
    INVALID = "invalid"
```

Invariants:

```text
CREATED      → reasons == (), artifact not None
NOT_ELIGIBLE → ("approval_consumption_artifact_not_eligible",), artifact None
INVALID      → ("approval_consumption_artifact_invalid_input",), artifact None
```

## Outcome mapping

```text
type(result) is OperatorApprovalConsumptionEligibilityResult required (exact, no subclass)

outcome == NO_GO AND type(reasons) is tuple AND len(reasons) >= 1 AND eligibility is None
    → NOT_ELIGIBLE
any other NO_GO shape (empty/list/subclass reasons, non-None eligibility)
    → INVALID                                  # malformed NO_GO is NOT accepted as NOT_ELIGIBLE
outcome == INVALID (incl. contradictory)
    → INVALID
outcome == ELIGIBLE
    → strict outer + nested validation below
unknown / non-result type
    → INVALID
```

## ELIGIBLE strict contract

Outer (each read once): `outcome is ELIGIBLE`, `type(reasons) is tuple`, `reasons == ()`,
`type(eligibility) is OperatorApprovalConsumptionEligibility` (exact, no subclass).

Nested scalars (each read once into a local):

```text
approval_intent_sha256        exact built-in lowercase hex64
evidence_sha256               exact built-in lowercase hex64
market                        exact built-in str == "KR"
symbol                        exact built-in str matching [0-9]{6}
evidence_evaluated_at         exact built-in aware ISO str
intent_declared_at            exact built-in aware ISO str
checked_at                    exact built-in aware ISO str
activation_authorized         is False
runtime_activation_outcome    exact built-in str == "no_go"
```

Time ordering: `evidence_evaluated_at <= intent_declared_at <= checked_at`.

Any nested failure → `INVALID / approval_consumption_artifact_invalid_input`, artifact None.

## Artifact schema

```python
OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION = 1
```

| field | source / value |
| --- | --- |
| `schema_version` | `1` (artifact-local owner) |
| `checked_at` | eligibility.checked_at |
| `approval_intent_schema_version` | shared `OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION` (= 1) |
| `approval_intent_sha256` | eligibility.approval_intent_sha256 |
| `candidate_evidence_schema_version` | shared `ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION` (= 2) |
| `candidate_evidence_sha256` | eligibility.evidence_sha256 |
| `market` | `"KR"` |
| `symbol` | eligibility.symbol |
| `evidence_evaluated_at` | eligibility.evidence_evaluated_at |
| `intent_declared_at` | eligibility.intent_declared_at |
| `activation_authorized` | `false` |
| `runtime_activation_outcome` | `"no_go"` |
| `eligibility_artifact_sha256` | canonical digest over the 12 fields above |

The intent/evidence schema versions are imported from their owning modules — **not** redefined
as artifact-local constants. Only `OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION`
is owned locally.

## Canonical hash

```python
operator_approval_consumption_eligibility_artifact_hash_payload(...)  # 12 fields
payload_sha256(...)  # decision.canonical_json
```

Deterministic, independently recomputable, lowercase hex64, sensitive to every field.

## Single-observation and isolation

```text
outer result fields (outcome, reasons, eligibility)  = read once each
nested eligibility fields                            = read once each
time parse                                           = once per timestamp
canonical hash payload                               = 1
payload_sha256                                       = 1
```

Zero: upstream eligibility rerun, intent verifier, evidence validator, clock read, config/env,
filesystem read/write, SQLite, network, broker/order, persistence, consumed marker.

Mutating the caller result/eligibility after locals are captured does not change the verdict or
artifact (single-read locals). Point-in-time concurrent atomicity is **not** claimed.

## Exception contract

Normal malformed input or unexpected `Exception` → sanitized `INVALID` (no raw exception/value/
path leakage). `MemoryError` / `KeyboardInterrupt` / `SystemExit` are re-raised.

## Carry-over test precision closure (4s)

- **H1**: exact built-in `datetime` + stateful custom `tzinfo` — first-read failure → INVALID;
  after the first `isoformat()` observation the caller tzinfo is never read again (detached parse).
- **H2**: `_binding_matches` defensive branches proven by single-field root-cause unit tests
  (intent/evidence schema version, evidence digest, intent/evidence market, intent/evidence symbol).
- **H3**: ELIGIBLE-path evidence canonical hash measured — `validate_activation_candidate_evidence_scalars`,
  `activation_candidate_evidence_hash_payload`, and the evidence-module `payload_sha256` each = 1.

## Standalone serialized verification (RTM-7c.4u)

A serialized artifact (this builder's output round-tripped through JSON) is independently verified
and converted to an immutable verified snapshot by
`composition.operator_approval_consumption_eligibility_artifact_verifier`. The builder and verifier
share these semantic owners exported from this module:

```text
validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed(...)  # single owner of the 12 content-field schema/binding/digest-shape/market/symbol/timestamp/posture/ordering checks
validate_operator_approval_consumption_eligibility_artifact_scalars_detailed(...)   # full 13-field owner: calls the content owner once, then validates stored digest shape
operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars(...)    # canonical 12-field hash owner
operator_approval_consumption_eligibility_artifact_hash_payload(...)                 # builder convenience, delegates to *_from_scalars (output unchanged)
```

Both the builder's ELIGIBLE path and the verifier's full validator call the **single content
owner** exactly once for their content-field semantics — neither re-implements the schema /
binding / market / symbol / timestamp / posture / ordering checks inline. Builder output (13
fields) and digest remain byte-equivalent to the pre-refactor result.

The verifier recomputes the digest from the **input payload values** of the 12 serialized content
fields and separately asserts the semantic constants — it does not auto-insert constants while
ignoring raw fields. It is a **consistency checker, not an authenticator**: a semantically valid
content change paired with a correctly recomputed digest verifies `VALID` by design (consistency),
whereas the same change with a stale stored digest is `INVALID`/`hash_mismatch`. VALID/snapshot
means schema·semantic·hash consistency only, never authenticity/provenance or actual consumption.
See `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`.

## Validated-content emission (RTM-7c.4v carry-over)

The builder's ELIGIBLE path now hashes and constructs the artifact from the **validated content
snapshot** (`content.validated`) returned by the shared content owner, never the raw caller locals
— so validation, hashing, and construction observe one identical source. Builder output (13 fields)
and the `eligibility_artifact_sha256` digest stay byte-equivalent to the pre-change result; the
content validator, canonical hash payload, and `payload_sha256` are each invoked exactly once, and
caller-payload mutation isolation is unchanged.

## Operator-facing verification CLI (RTM-7c.4v)

The RTM-7c.4u verifier API is exposed read-only via
`ops/run_paper_fast_loop.py --verify-approval-consumption-eligibility-artifact --json` (stdin-only,
no config/env/clock/DB/filesystem write). Same consistency-not-authenticity semantics and constant
NO-GO posture. See
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_VERIFICATION_CLI_CONTRACT.md`.

## Still OPEN (unchanged posture)

Approval consumption, consumed marker, replay/nonce/idempotency, signing/HMAC, Operator identity
authentication, artifact persistence/file output, intent/evidence lookup, TTL/freshness
re-evaluation, activation caller/token, `--run`, KIS/network, broker/order, operational DB write,
daemon/scheduler, unattended pilot, default runtime activation beyond constant NO-GO observation.
