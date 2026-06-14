# Operator Approval Intent Contract (RTM-7c.4o)

Read-only **canonical Operator approval intent** for the paper fast-loop attended activation
lane. Freezes one combined freshness-qualified mechanical PASS with CREATED canonical evidence
into a single immutable approval-intent digest that a *future* approval consumer can reference.

**Runtime activation: NO-GO.** An approval intent is **not** Operator identity authentication,
signing/HMAC, writer-stop machine proof, approval consumption, replay prevention, an activation
token, or runtime activation authorization. The activation posture is a constant NO-GO on every
path.

Code: `composition.operator_approval_intent.build_operator_approval_intent`

## What the intent means

> The caller declared — via three explicit manual confirmations — that they approve the attended
> paper-fast-loop candidate bound to a specific canonical evidence digest, and that they have
> manually confirmed writer-stop and live-order prohibition conditions.

The three confirmation booleans are **manual declarations**, not machine proof:

```text
operator_approval_declared
writers_stopped_manually_confirmed
live_orders_forbidden_confirmed
```

Each must be the exact built-in `True`. `False`, `0`, `1`, `None`, `"true"`, arbitrary objects,
and non-exact bool types fail closed.

## What the intent does **not** mean

- Operator identity authentication
- Electronic signature / HMAC
- Proof that writers actually stopped
- Approval consumption (this lane defines only; it does not consume)
- Replay / nonce / idempotency
- Activation token / runtime activation authorization
- Order execution permission
- Persistence (the digest is returned in-memory; no file or DB write)

## Generation scope (combined PASS + CREATED evidence only)

Intent is created **only** when:

```text
type(combined_result) is FreshnessQualifiedEvidenceResult
combined_result.outcome           = PASS
combined_result.reasons           = ()
type(evidence_result) is ActivationCandidateEvidenceResult
evidence_result.outcome           = CREATED
evidence_result.reasons           = ()
type(evidence) is ActivationCandidateEvidence
evidence passes full schema-v2 semantic contract via validate_activation_candidate_evidence_scalars
  (matching evidence_sha256 alone is insufficient)
  schema_version                    = exact built-in int == 2
  evaluated_at                      = exact built-in str, timezone-aware ISO
  receipt_sha256                    = lowercase hex64
  fresh_precheck_receipt_sha256     = lowercase hex64
  evidence_sha256                   = lowercase hex64 (independent recomputation matches)
  market                            = KR
  symbol                            = ASCII [0-9]{6}
  max_age_microseconds              = exact non-negative built-in int
  receipt_age_microseconds          = exact non-negative built-in int
  receipt_age_microseconds          <= max_age_microseconds
  final_preflight_outcome           = "pass"
  freshness_outcome                 = "fresh"
  fresh_precheck_executed           = exact True
  receipt_age_evaluated             = exact True
  freshness_policy_evaluated        = exact True
  activation_authorized             = exact False
  runtime_activation_outcome        = "no_go"
declared_at                       = exact built-in tz-aware datetime
declared_at                       >= evidence.evaluated_at (strict parse)
all three manual confirmations    = exact True
```

Combined `NO_GO` → `NOT_ELIGIBLE` (`approval_intent_not_eligible`). A contradictory combined
`PASS` (non-empty reasons, missing/`None`/wrong-type evidence result, evidence not `CREATED`,
semantically invalid evidence even with matching hash, hash mismatch, invalid declarations, or
invalid `declared_at`) → `INVALID` (`approval_intent_invalid_input`).

## Intent model

```text
OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION = 1

OperatorApprovalIntent (frozen):
  schema_version:                         int
  declared_at:                            str      # caller time, exact ISO string
  evidence_schema_version:                int      # bound evidence schema (2)
  evidence_sha256:                        str      # lowercase hex64 — bound evidence digest
  market:                                 str
  symbol:                                 str
  approval_scope:                         str      # constant "attended_paper_fast_loop_candidate"
  operator_approval_declared:             bool     # constant True when CREATED
  writers_stopped_manually_confirmed:       bool     # constant True when CREATED
  live_orders_forbidden_confirmed:          bool     # constant True when CREATED
  activation_authorized:                  bool     # constant False
  runtime_activation_outcome:             str      # constant "no_go"
  approval_intent_sha256:                 str      # canonical digest over the other 12 fields
```

`approval_scope` is fixed — callers cannot choose an arbitrary scope string.

**Never stored:** Operator identity, signature/HMAC fields, raw evidence payload, receipt bodies,
artifact paths, config paths, secret/env data. The `evidence_sha256` binds the evidence digest.

## Canonical hash

Reuses `decision.canonical_json.payload_sha256`. Hash input (sorted keys via canonical JSON):

```text
schema_version
declared_at
evidence_schema_version
evidence_sha256
market
symbol
approval_scope
operator_approval_declared
writers_stopped_manually_confirmed
live_orders_forbidden_confirmed
activation_authorized
runtime_activation_outcome
```

Excluded: `approval_intent_sha256`.

Independent recomputation: `asdict(intent)` minus `approval_intent_sha256` → `payload_sha256(...)`.

## Builder API

```python
build_operator_approval_intent(
    *,
    combined_result: FreshnessQualifiedEvidenceResult,
    declared_at: datetime,
    operator_approval_declared: bool,
    writers_stopped_manually_confirmed: bool,
    live_orders_forbidden_confirmed: bool,
) -> OperatorApprovalIntentResult
```

Pure function constraints:

- No clock read (caller supplies `declared_at`)
- No env/config/filesystem/network/SQLite access
- No receipt verifier / precheck / evaluator / evidence-builder re-invocation
- No persistence / file output

## Single-observation rule

The builder reads each caller-owned object once into locals and reuses those locals for
validation and the hash payload. Malformed or deleted fields fail closed via `AttributeError`
catch → `INVALID`.

Production validation does **not** use `dataclasses.asdict` or `copy.deepcopy` on
caller-owned evidence. Caller-defined `__deepcopy__` hooks are never invoked. After the
single-read snapshot, strict semantic validation runs via the shared
`validate_activation_candidate_evidence_scalars` helper (same contract as
`validate_activation_candidate_evidence_object` in the evidence module). Validation reuses
the detached locals for canonical hash recomputation — the caller evidence object is not
re-read afterwards.

## Evidence semantic contract (RTM-7c.4o closure)

Matching `evidence_sha256` alone is **insufficient**. Approval intent requires the full
evidence schema v2 semantic contract:

- Both receipt hashes must be lowercase hex64
- `final_preflight_outcome` must be exactly `"pass"` and `freshness_outcome` exactly `"fresh"`
- All three observation flags (`fresh_precheck_executed`, `receipt_age_evaluated`,
  `freshness_policy_evaluated`) must be exact built-in `True`
- Receipt age must not exceed explicit max-age
- Rejects bool/float/string schema versions, ages, flags, unknown outcomes, and enum/object
  substitutes for expected exact strings

Intent remains unauthenticated, unconsumed, unpersisted, and NO-GO.

## Integration scope (this lane)

The existing CLI mode `--freshness-preflight-activation-candidate` is unchanged — it still emits
evidence digest fields only. Approval-intent CLI input is a separate future lane.

## Related contracts

- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_EVIDENCE_CONTRACT.md` — evidence schema v2 / hash binding
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition wiring root

## Still OPEN (unchanged posture)

Operator approval consumption, intent persistence/file output, signing/HMAC, Operator identity
authentication, replay/nonce/idempotency, approval CLI input, activation token/caller, `--run`,
KIS/network, broker/order, operational DB write, writer-stop machine proof, default runtime
activation beyond constant NO-GO.
