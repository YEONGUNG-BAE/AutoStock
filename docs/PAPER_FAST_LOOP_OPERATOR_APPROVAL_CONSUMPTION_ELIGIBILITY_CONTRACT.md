# Operator Approval Consumption Eligibility Contract (RTM-7c.4s)

Pure preflight API that judges whether a **verified** approval intent and a **validated**
`ActivationCandidateEvidence` object **could** be combined as consumption candidates.

**This is not approval consumption.** No consumed marker, replay protection, persistence,
Operator identity authentication, signature/HMAC, intent TTL, evidence freshness re-evaluation,
or runtime activation authorization.

**Runtime activation: NO-GO** on every path (`activation_authorized=false`,
`runtime_activation_outcome="no_go"`).

Code: `composition.operator_approval_consumption_eligibility.assess_operator_approval_consumption_eligibility`

## What eligibility means

> After strict `now` snapshot, one intent verify/snapshot pass, one evidence validate pass,
> digest/identity binding, and time ordering, the pair is observationally combinable as a
> future consumption candidate.

## What eligibility does **not** mean

- Actual approval consumption
- Consumed / replayed state
- Nonce / idempotency
- Signing / HMAC / Operator identity authentication
- Intent TTL / max approval age
- Evidence freshness re-evaluation at `now`
- Persistence (no file/DB write)
- Activation authorization
- Evidence or intent authentication beyond schema/semantic/hash validation

## API

```python
assess_operator_approval_consumption_eligibility(
    *,
    intent_payload: object,
    evidence: object,
    now: object,
) -> OperatorApprovalConsumptionEligibilityResult
```

Processing precedence (fixed):

```text
1. now strict snapshot
2. intent verify/snapshot (verify_and_snapshot_operator_approval_intent, exactly once)
3. evidence scalar snapshot + semantic/hash validation (exactly once)
4. intent ↔ evidence binding (schema v2, digest, market, symbol)
5. time ordering (evidence.evaluated_at ≤ intent.declared_at ≤ now)
6. ELIGIBLE
```

## Outcome model

```python
class OperatorApprovalConsumptionEligibilityOutcome(StrEnum):
    ELIGIBLE = "eligible"
    NO_GO = "no_go"
    INVALID = "invalid"
```

Invariants:

```text
ELIGIBLE → reasons == (), eligibility is not None
NO_GO    → len(reasons) >= 1, eligibility is None
INVALID  → len(reasons) == 1, eligibility is None
```

## `now` strict snapshot

Caller-owned exact built-in timezone-aware `datetime` only. Flow:

```text
now → isoformat() exactly once → fromisoformat() → parsed built-in aware datetime
```

Invalid `now` → `INVALID` / `approval_consumption_invalid_now`. No clock read inside the API.

## Intent verification

Uses `verify_and_snapshot_operator_approval_intent` exactly once. Any non-VALID intent →
`INVALID` / `approval_consumption_intent_invalid`. Raw intent payload is not retained.

Intent semantic validation is owned by shared `validate_operator_approval_intent_scalars_detailed`
(single pass inside the verifier core — no duplicate semantic helpers).

## Evidence verification

Exact `ActivationCandidateEvidence` type only. Shared `validate_activation_candidate_evidence_scalars`
(schema v2 full semantic contract + canonical hash parity). Invalid →
`INVALID` / `approval_consumption_evidence_invalid`. Caller evidence object is not re-read after
snapshot.

## Binding

All must match on validated exact built-in scalars:

```text
intent.evidence_schema_version == evidence.schema_version == 2
intent.evidence_sha256 == evidence.evidence_sha256
intent.market == evidence.market
intent.symbol == evidence.symbol
```

Mismatch → `NO_GO` / `approval_consumption_evidence_mismatch`.

## Time ordering

Validated parsed ISO datetimes only:

```text
evidence.evaluated_at ≤ intent.declared_at ≤ now
```

| Failure | Reason |
|---------|--------|
| `intent.declared_at < evidence.evaluated_at` | `approval_consumption_intent_precedes_evidence` |
| `intent.declared_at > now` | `approval_consumption_intent_in_future` |

No TTL, max approval age, freshness re-evaluation, or clock-skew allowance in this lane.

## Single-execution contract (per ELIGIBLE call)

```text
now snapshot                      = 1
intent payload snapshot           = 1
intent semantic validation        = 1
intent hash recomputation         = 1
evidence object scalar snapshot   = 1
evidence semantic validation      = 1
evidence hash recomputation       = 1
```

Forbidden: standalone intent verifier re-call, public snapshot re-call, evidence builder,
receipt verifier/precheck/freshness pipeline, raw object re-read.

## Isolation

Zero: clock read, config/env, filesystem read/write, SQLite, network, broker/order,
thread/process, persistence, consumption marker.

## Downstream artifact (RTM-7c.4t)

A separate pure builder freezes an `ELIGIBLE` result into a canonical immutable artifact —
see `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`. The
artifact is a canonical observation, **not** consumption: no consumed marker, replay prevention,
persistence, authentication/signature, TTL/freshness re-evaluation, or activation authorization.
Malformed `NO_GO` results map to `INVALID` (not `NOT_ELIGIBLE`). Runtime activation stays NO-GO.
4s carry-over H1–H3 test precision is closed under 4t.

## Carry-over H1 — intent semantic validation single owner

`validate_operator_approval_intent_scalars_detailed` owns field-level stable reason classification,
full semantic validation, and validated scalar snapshot creation. Verifier core
`_verify_detached_operator_approval_intent` calls it exactly once (no duplicate declared-at /
evidence-binding / identity / declaration / posture helpers).

## Downstream artifact + verification (RTM-7c.4t / 7c.4u)

An `ELIGIBLE` result is frozen into a canonical immutable artifact (4t,
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`); a serialized
artifact is independently verified and converted to an immutable verified snapshot (4u,
`PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md`). Both
remain observations only — schema·semantic·hash consistency, never actual consumption,
persistence, authentication, or activation authorization. Constant NO-GO.

## Still OPEN (unchanged posture)

Approval consumption, consumed marker, replay/nonce/idempotency, signing/HMAC, Operator identity
authentication, intent persistence/file output, intent TTL/freshness policy, activation
caller/token, `--run`, KIS/network, broker/order, operational DB write, daemon/scheduler,
unattended pilot, default runtime activation beyond constant NO-GO observation fields.
