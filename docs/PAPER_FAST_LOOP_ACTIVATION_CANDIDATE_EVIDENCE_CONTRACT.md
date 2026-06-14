# Activation Candidate Evidence Contract (RTM-7c.4n)

Read-only **canonical freshness-qualified candidate evidence** for the paper fast-loop.
Freezes one freshness-qualified mechanical PASS into a single immutable canonical payload that
a *future* Operator-approval stage can reference.

**Runtime activation: NO-GO.** An evidence digest is **not** receipt authenticity, a
signature/HMAC, Operator approval, writer-stop proof, an activation token, or activation
authorization. The activation posture is a constant NO-GO on every path.

Code: `composition.activation_candidate_evidence.build_activation_candidate_evidence`

## What the evidence means

> A specific verified receipt, a specific explicit max-age policy, a specific caller time, and
> a specific final-preflight/freshness result were combined into one canonical payload.

## What the evidence does **not** mean

- Receipt authenticity / signing / HMAC
- Operator approval (input, storage, or consumption)
- Writer-stop proof
- Activation token / runtime activation authorization
- Threshold selection / TTL calibration
- Persistence (the digest is returned in-memory; no file or DB write)

## Generation scope (PASS/FRESH only)

Evidence is created **only** when:

```text
freshness-qualified outcome   = PASS
freshness evaluation          = FRESH
final preflight outcome       = PASS
activation_authorized         = false
runtime_activation_outcome    = no_go
```

`NO_GO`/`STALE` produce **no** digest (`evidence = None`). There is no generic failure
evidence.

## Evidence model

```text
ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION = 1

ActivationCandidateEvidence (frozen):
  schema_version:              int
  evaluated_at:                str      # caller now, exact ISO string
  receipt_sha256:              str      # lowercase hex64
  market:                      str
  symbol:                      str
  max_age_microseconds:        int
  receipt_age_microseconds:    int
  final_preflight_outcome:     str      # "pass"
  freshness_outcome:           str      # "fresh"
  fresh_precheck_executed:     bool
  receipt_age_evaluated:       bool
  freshness_policy_evaluated:  bool
  activation_authorized:       bool     # constant False
  runtime_activation_outcome:  str      # constant "no_go"
  evidence_sha256:             str      # canonical digest over the other 14 fields
```

**Never stored:** raw receipt payload, artifact path, config path, fingerprint raw payload,
secret/env data, approval fields, signature fields. The receipt hash already binds the
original receipt and its artifact observation, so the raw fingerprint is not duplicated.

## Canonical hash

Reuses `decision.canonical_json.payload_sha256` (canonical JSON, sorted keys, no floats).
The hash payload is **every evidence field except `evidence_sha256`** (14 fields):

```text
schema_version
evaluated_at
receipt_sha256
market
symbol
max_age_microseconds
receipt_age_microseconds
final_preflight_outcome
freshness_outcome
fresh_precheck_executed
receipt_age_evaluated
freshness_policy_evaluated
activation_authorized
runtime_activation_outcome
```

The constant NO-GO posture fields are part of the digest, so a tampered posture changes the
hash. Independent recomputation: take `asdict(evidence)`, drop `evidence_sha256`, and
`payload_sha256(...)` — the result equals the stored digest.

## `evaluated_at` contract

`evaluated_at` is the **same** caller `now` passed to the freshness-qualified call — the
builder reads no clock of its own and must be given a timezone-aware `datetime` (validated via
the shared `final_preflight_now_is_invalid` guard). The composition wrapper passes one `now`
to both the qualified call and the evidence builder; the two stages never read different clocks.

## Builder API

```python
build_activation_candidate_evidence(
    *,
    qualified_result: ActivationCandidateFreshnessPreflightResult,
    evaluated_at: datetime,
) -> ActivationCandidateEvidenceResult
```

```text
ActivationCandidateEvidenceOutcome = { CREATED, NOT_ELIGIBLE, INVALID }

ActivationCandidateEvidenceResult:
  outcome:   CREATED | NOT_ELIGIBLE | INVALID
  reasons:   tuple[str, ...]
  evidence:  ActivationCandidateEvidence | None
```

Stable reasons: `candidate_evidence_not_eligible`, `candidate_evidence_invalid_input`. Raw
object/type/value never appear in reasons.

### Composition wrapper

```python
freshness_qualify_and_build_candidate_evidence(
    *, settings, receipt_payload, now, policy, base_dir=None,
) -> FreshnessQualifiedEvidenceResult   # { qualified_result, evidence_result }
```

Runs the existing `freshness_qualify_activation_candidate`, then invokes the builder **only**
on a qualified PASS (`evaluated_at = now`). A qualified `NO_GO` returns `evidence_result =
None` — the builder is not called. Per call: receipt verifier 1, receipt snapshot 1, fresh
precheck 1, freshness evaluator 1, evidence builder 1, clock read 1. No extra observation.

## Processing order and strict eligibility

1. Exact result type — `type(qualified_result) is ActivationCandidateFreshnessPreflightResult`
   (subclass/wrong object → `INVALID`)
2. `evaluated_at` strict tz-aware guard (invalid → `INVALID`)
3. Well-formed non-PASS verdict (`outcome != PASS`) → `NOT_ELIGIBLE`
4. PASS invariants (any failure → `INVALID`, never a silent digest):
   - `reasons == ()`
   - `final_preflight_result` and `freshness_evaluation` not `None`
   - `freshness_policy_evaluated is True`; `freshness_evaluation.freshness_policy_evaluated is
     True`
   - `final_preflight_result.outcome == PASS`; `fresh_precheck_executed is True`;
     `receipt_age_evaluated is True`
   - `freshness_evaluation.outcome == FRESH`
   - `receipt_sha256` exact lowercase hex64
   - `market` / `symbol` exact non-empty `str`
   - `receipt_age_microseconds` / `max_age_microseconds` exact non-negative `int`;
     `receipt_age <= max_age`
   - `activation_authorized is False`; `runtime_activation_outcome == "no_go"`
5. Build canonical digest → `CREATED`

A malformed exact-type result (e.g. a deleted field) fails closed via `AttributeError` catch →
`INVALID`. A contradictory synthetic PASS (e.g. `activation_authorized=True`, freshness STALE,
or `age > max_age`) is `INVALID`, never `CREATED`.

## Single-observation principle

The builder reads each caller-owned nested object/scalar **once** into a local and reuses that
local for validation and the hash payload. It does not re-observe nested result fields.

## CLI integration (no new mode)

Existing mode `--freshness-preflight-activation-candidate` gains two optional PASS fields:

```text
candidate_evidence_sha256        # lowercase hex64 on CREATED PASS, else null
candidate_evidence_schema_version # 1 on CREATED PASS, else null
```

`NO_GO`/`STALE`/input-failure → both `null`. The CLI runs the qualified preflight **once**
and reuses the same KST `now` for the evidence `evaluated_at` (no second qualified run, no
extra clock read). Output stays path-free: no full evidence raw payload, config path, artifact
path, fingerprint body, raw receipt, raw `checked_at`, or secret/env identifier. The evidence
digest is documented as **not** approval.

CLI exit codes are unchanged (PASS 0, NO_GO 1, `--run` 2). A digest on a PASS is still
`activation_authorized=false`, `runtime_activation_outcome="no_go"`.

## Activation posture (every path)

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
explicit_operator_approval_required = true
writers_stopped_manual_confirmation_required = true
```

## Out of scope (this lane)

- Operator approval input/storage/consumption; approval-evidence binding
- Signing / HMAC
- Evidence persistence / file output
- Default/config/env max-age; threshold calibration
- Activation caller/token; `--run` implementation
- KIS / network; broker/order
- Operational DB write; schema migration/reconcile
- Daemon / scheduler / process lock; unattended pilot

## Related contracts

- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FRESHNESS_PREFLIGHT_CONTRACT.md` — qualified PASS source
- `PAPER_FAST_LOOP_RECEIPT_FRESHNESS_POLICY_CONTRACT.md` — explicit FRESH/STALE evaluator
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FINAL_PREFLIGHT_CONTRACT.md` — final-preflight PASS core
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
- `docs/TECH_DEBT.md` (OPEN items)
