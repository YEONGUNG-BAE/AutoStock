# Verified Operator Approval Consumption Eligibility Artifact Contract (RTM-7c.4u)

Standalone verification of an untrusted **serialized** Operator approval-consumption eligibility
artifact (RTM-7c.4t output round-tripped through JSON), plus conversion of a valid artifact into an
immutable **verified snapshot** of the 13 canonical scalar fields. API-only boundary — no CLI,
no persistence, no file input in this lane.

**Runtime activation: NO-GO.** A `VALID` verdict / verified snapshot is an artifact
schema·semantic·hash consistency observation — not actual approval consumption, authentication,
signature, replay prevention, persistence, freshness/TTL re-evaluation, or activation
authorization.

Code:

- `composition.operator_approval_consumption_eligibility_artifact_verifier.verify_operator_approval_consumption_eligibility_artifact_payload`
- `composition.operator_approval_consumption_eligibility_artifact_verifier.verify_and_snapshot_operator_approval_consumption_eligibility_artifact`
- shared semantic owner `validate_operator_approval_consumption_eligibility_artifact_scalars_detailed` (RTM-7c.4t module)
- shared canonical hash owner `operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars` (RTM-7c.4t module)

## What VALID / a verified snapshot means

> The input is an exact built-in `dict` with exactly the 13 canonical fields, exact scalar types,
> the expected semantic constants, timezone-aware timestamps in
> `evidence_evaluated_at <= intent_declared_at <= checked_at` order, and an
> `eligibility_artifact_sha256` that exactly equals a recomputation over the actual 12 serialized
> content fields. The returned `VerifiedOperatorApprovalConsumptionEligibilityArtifact` holds exact
> built-in immutable scalars copied from one detached observation.

## What VALID / a verified snapshot does **not** mean

- Actual approval consumption
- Consumed marker creation
- Replay / nonce / idempotency prevention
- Persistence (no file/DB write)
- Authentication / signature / HMAC
- TTL / freshness re-evaluation
- Activation authorization (`activation_authorized` stays `false`; `runtime_activation_outcome`
  stays `"no_go"`)

## Snapshot model (exactly 13 fields)

```python
@dataclass(frozen=True)
class VerifiedOperatorApprovalConsumptionEligibilityArtifact:
    schema_version: int
    checked_at: str
    approval_intent_schema_version: int
    approval_intent_sha256: str
    candidate_evidence_schema_version: int
    candidate_evidence_sha256: str
    market: str
    symbol: str
    evidence_evaluated_at: str
    intent_declared_at: str
    activation_authorized: bool
    runtime_activation_outcome: str
    eligibility_artifact_sha256: str
```

Field set matches schema v1 exactly. Snapshot excludes: raw payload, original dict/list,
config/path, verification exception, authentication state, consumption state, persistence state.

## Verdict / result models

```python
class OperatorApprovalConsumptionEligibilityArtifactVerification:
    outcome: ...VerificationOutcome  # valid | invalid
    schema_version: int | None
    approval_intent_schema_version: int | None
    approval_intent_sha256: str | None
    candidate_evidence_schema_version: int | None
    candidate_evidence_sha256: str | None
    eligibility_artifact_sha256: str | None
    reason_codes: tuple[str, ...]

class VerifiedOperatorApprovalConsumptionEligibilityArtifactResult:
    outcome: ...VerificationOutcome
    reason_codes: tuple[str, ...]
    snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact | None
```

Invariants:

```text
VALID   → reason_codes == (), snapshot is not None
INVALID → len(reason_codes) == 1, snapshot is None
```

## Stable invalid reason codes

```text
eligibility_artifact_not_object
eligibility_artifact_unknown_field
eligibility_artifact_missing_field
eligibility_artifact_unsupported_schema
eligibility_artifact_invalid_field
eligibility_artifact_invalid_timestamp
eligibility_artifact_invalid_binding
eligibility_artifact_invalid_activation_posture
eligibility_artifact_invalid_time_ordering
eligibility_artifact_hash_mismatch
```

One root cause → exactly one reason. Reason codes never embed raw field value, hash, datetime,
path, or exception text.

## Detached payload snapshot

```text
caller payload
→ type(payload) is dict            (dict subclass rejected → not_object)
→ tuple(payload.items())           (observed exactly once)
→ type(key) is str for each key    (non-exact/custom key → unknown_field, no hash/eq hook)
→ canonical 13-field set check     (unknown / duplicate → unknown_field; missing → missing_field)
→ new built-in dict (scalar references copied)
→ caller payload never re-accessed
```

Caller payload mutation after snapshot cannot change the verdict or the returned snapshot
(point-in-time observation; not a concurrent-atomicity guarantee).

## Single-pass shared verification core

Both public APIs share one detached path:

```text
_snapshot_artifact_payload (exactly once)
→ validate_operator_approval_consumption_eligibility_artifact_scalars_detailed (exactly once)
→ canonical hash recomputation via *_hash_payload_from_scalars + payload_sha256 (exactly once)
```

The verifier recomputes the canonical digest from the **input payload values** (the 12 serialized
content fields, including the semantic constants) and *separately* asserts those constants equal
the expected values — it does not blindly auto-insert constants while ignoring the raw fields.

Per normal VALID call:

```text
payload snapshot              = 1
field-set validation          = 1
semantic scalar validation    = 1
timestamp parse               = 1 each
canonical hash payload        = 1
payload_sha256                = 1
upstream builder/eligibility/intent-verifier/evidence rerun = 0
clock / filesystem / network  = 0
caller payload re-read         = 0
```

| API | Returns |
|-----|---------|
| `verify_operator_approval_consumption_eligibility_artifact_payload` | verdict only |
| `verify_and_snapshot_operator_approval_consumption_eligibility_artifact` | verdict + immutable snapshot |

## Builder → verifier invariant

Every RTM-7c.4t builder `CREATED` artifact serialized to a JSON-compatible dict MUST verify
`VALID` and produce a snapshot whose 13 fields exactly match the builder artifact (real seeded
eligibility path included).

## Verifier ↔ snapshot parity

For the same payload the two APIs MUST agree on `outcome`, `reason_codes`, and the verified
schema/digest metadata. VALID adds the immutable snapshot only on the snapshot API. The snapshot
path uses the shared detached core directly — it never re-invokes the public verifier.

## Exception contract

| Input / condition | Behavior |
|-------------------|----------|
| Malformed / semantic-invalid input | stable `INVALID` + single reason |
| `MemoryError` / `KeyboardInterrupt` / `SystemExit` | re-raised |
| Other unexpected `Exception` | stable `INVALID` / `eligibility_artifact_invalid_field` |

No raw exception text, traceback, or path escapes into the verdict.

## 13-field serialized tamper coverage

Each of the 13 serialized fields is independently tampered (root type, dict subclass, unknown /
missing / non-exact-string key, schema versions, three digests with malformed/uppercase/short/
subclass/bytes/None/int/stale variants, market/symbol identity, three timestamps incl. naive /
non-string / ordering, posture, and each content field recomputed) — every tamper stays `INVALID`.
A semantically invalid field with an independently recomputed digest still verifies `INVALID`.

## Still OPEN (unchanged posture)

Artifact persistence / file output, actual approval consumption, consumed marker, replay / nonce /
idempotency, signing / HMAC, Operator identity authentication, intent/evidence lookup, TTL /
freshness re-evaluation, activation token/caller, `--run`, KIS/network, broker/order, operational
DB write, schema migration/reconcile, daemon/scheduler, unattended pilot. No new CLI in this lane;
a stdin/file CLI is deferred to the lane that introduces artifact persistence or external file
input.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4t builder + shared scalar/hash owners
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_CONTRACT.md` — RTM-7c.4s eligibility preflight
- `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_INTENT_CONTRACT.md` — RTM-7c.4r verified intent snapshot (sibling verifier pattern)
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root
