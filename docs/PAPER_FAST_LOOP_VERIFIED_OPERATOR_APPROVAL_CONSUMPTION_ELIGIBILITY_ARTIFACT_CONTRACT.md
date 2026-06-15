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
- shared content semantic owner `validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed` (RTM-7c.4t module) — single owner of the 12 content-field schema / binding / digest-shape / market / symbol / timestamp / posture / ordering checks, shared by both the builder and the full validator
- full semantic owner `validate_operator_approval_consumption_eligibility_artifact_scalars_detailed` (RTM-7c.4t module) — calls the content owner exactly once, then validates the stored `eligibility_artifact_sha256` digest shape
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

## Verification semantics: consistency, not authenticity

The verifier is a **consistency checker**, not an authenticator. It confirms that a payload is
well-formed, semantically valid, and that its stored digest matches a digest recomputed over the
12 content fields. It does **not** prove provenance, authenticity, or that the artifact was
produced by the trusted builder — there is no signature, HMAC, or origin check in this lane.

A tampered field therefore falls into one of three categories:

| Category | Example | Verdict |
|----------|---------|---------|
| (A) Malformed / semantic-invalid | bad root type, unknown/missing key, unsupported schema, non-`KR` market, non-6-digit symbol, naive/non-string/out-of-order timestamp, wrong posture, malformed digest hex | `INVALID` + single reason |
| (B) Semantic-valid content change + **stale** stored digest | a different valid symbol/digest/`checked_at` whose `eligibility_artifact_sha256` was not recomputed | `INVALID` / `eligibility_artifact_hash_mismatch` |
| (C) Semantic-valid content change + **recomputed** digest | a different valid symbol/digest/`checked_at` whose digest is correctly recomputed over the change | `VALID` (by design — consistency holds; authenticity is out of scope) |

Tests cover each category independently across root type, dict subclass, unknown / missing /
non-exact-string key, schema versions, three digests, market/symbol identity, three timestamps,
posture, ordering, stale-digest, and recomputed-digest variants. Category (C) is **expected
`VALID`**: a correctly recomputed payload is observationally consistent, and consistency is the
only property this lane verifies.

## Operator-facing CLI (RTM-7c.4v)

This verifier API is exposed read-only via
`ops/run_paper_fast_loop.py --verify-approval-consumption-eligibility-artifact --json` — stdin-only,
bounded parser, no config/env/clock/DB/filesystem write, verifier called exactly once. The CLI
carries the same consistency-not-authenticity semantics (Category C recomputed payload → VALID) and
constant NO-GO posture, and separates three outcomes: **FAIL** (CLI/argument/input boundary —
verification not started, verifier `0`), **INVALID** (verifier rejected the artifact), **VALID**
(consistency only). See
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_VERIFICATION_CLI_CONTRACT.md`.

## Still OPEN (unchanged posture)

Artifact persistence / file output, actual approval consumption, consumed marker, replay / nonce /
idempotency, signing / HMAC, Operator identity authentication, intent/evidence lookup, TTL /
freshness re-evaluation, activation token/caller, `--run`, KIS/network, broker/order, operational
DB write, schema migration/reconcile, daemon/scheduler, unattended pilot. A stdin/file CLI for
external file input (vs. the 4v stdin-only mode) is deferred to the lane that introduces artifact
persistence.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4t builder + shared scalar/hash owners
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_CONTRACT.md` — RTM-7c.4s eligibility preflight
- `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_INTENT_CONTRACT.md` — RTM-7c.4r verified intent snapshot (sibling verifier pattern)
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_CONTRACT.md` — RTM-7c.4w canonical persistence-payload encode/decode (consumes this verified snapshot; API-only, no file I/O; decoder requires exact canonical bytes; shared result-invariant helpers exported from this module)
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root
