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

**RTM-7c.4o declared-time snapshot closure:** `snapshot_declared_at` freezes caller
`declared_at` via a detached single observation — `isoformat()` exactly once, then
`fromisoformat()` into a parsed built-in datetime. Comparison and hash payload use the
snapshot ISO/parsed values only; the builder never re-reads the caller datetime or caller
tzinfo. Custom/stateful tzinfo that raises on a second `utcoffset()` call cannot escape
after snapshot; a first-call raise → `INVALID`. A tzinfo that changes offset after snapshot
does not affect verdict/hash within a single build.

**RTM-7c.4o combined-PASS qualified consistency closure:** A combined `PASS` also requires
`type(qualified_result) is ActivationCandidateFreshnessPreflightResult` with:

```text
qualified.outcome                              = PASS
qualified.reasons                              = ()
qualified.receipt_sha256                       = validated evidence.receipt_sha256
qualified.market                               = validated evidence.market
qualified.symbol                               = validated evidence.symbol
qualified.freshness_policy_evaluated           = exact True
qualified.activation_authorized                = exact False
qualified.runtime_activation_outcome           = "no_go"
qualified.explicit_operator_approval_required  = exact True
qualified.writers_stopped_manual_confirmation_required = exact True
```

Evidence full semantic contract remains owned by the shared
`validate_activation_candidate_evidence_scalars` helper — the builder does not re-validate
nested final/freshness machine proof. Combined PASS with contradictory qualified result
(e.g. qualified `NO_GO`, identity mismatch, wrong object/subclass, posture mismatch) →
`INVALID`.

**RTM-7c.4o strict result-scalar comparison closure:** Exact result dataclass type alone
does not trust field runtime types. PASS-path reasons (`combined_reasons`, `er_reasons`,
`qr_reasons`) require exact built-in empty tuple via `_is_exact_empty_reasons` — never
`value == ()` / `value != ()` (caller `__eq__`/`__ne__` hooks are not invoked). Qualified
identity/runtime scalars (`receipt_sha256`, `market`, `symbol`, `runtime_activation_outcome`)
require `type(value) is str` before built-in string equality; str subclass, arbitrary object,
custom always-equal, or raising comparator → `INVALID` with no exception escape. Malformed
comparison objects surface only the stable reason `approval_intent_invalid_input`.

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

**Declared-at snapshot:** `snapshot_declared_at(declared_at)` calls caller `isoformat()`
exactly once, re-parses to a timezone-aware built-in datetime, and freezes `(canonical_iso,
parsed_datetime)`. Time comparison uses `parsed_datetime` only; intent model and hash use
`canonical_iso` only. No post-snapshot access to caller datetime or caller tzinfo.

**Combined qualified snapshot:** `combined_outcome`, `combined_reasons`, `qualified_result`,
and `evidence_result` are read once from the combined result; qualified identity/posture
scalars are read once from `qualified_result`. No post-validation re-read of caller
combined/qualified objects.

**Strict scalar comparison:** `_is_exact_empty_reasons` validates PASS-path reasons as
exact built-in empty tuple (no `== ()`). Qualified string scalars use `type(value) is str`
before equality — caller-defined comparison hooks are never invoked.

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

## RTM-7c.4o declared-time and qualified-consistency closure

- Caller `declared_at` is frozen by detached single observation (`snapshot_declared_at`).
- Custom/stateful tzinfo state changes after snapshot do not affect verdict/hash within a build.
- Combined `PASS` requires consistent qualified `PASS` identity/posture (receipt hash, market,
  symbol, freshness-policy flag, constant NO-GO posture, approval/writer flags).
- Evidence semantic validation remains owned by the shared evidence validator.
- Intent remains unauthenticated, unconsumed, unpersisted; activation remains NO-GO.

## RTM-7c.4o strict result-scalar comparison closure

- Exact result dataclass type does not trust field runtime types.
- PASS reasons require exact built-in empty tuple (`_is_exact_empty_reasons`).
- Qualified identity/runtime requires exact built-in `str` before equality.
- Caller-defined equality hooks are not used in validation.
- Malformed comparison objects → stable `approval_intent_invalid_input`.
- Intent remains unauthenticated, unconsumed, unpersisted; activation remains NO-GO.

## Integration scope (this lane)

**RTM-7c.4p** adds CLI mode `--build-operator-approval-intent` — explicit `--json` and `--config`
(no default config fallback), manual confirmation flags, stable JSON envelope (`mode` +
`reasons` only), FAIL vs NO_GO taxonomy; stdout intent only (no persistence/consumption).
See `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CLI_CONTRACT.md`.

**RTM-7c.4q** adds shared scalar/hash helpers (`operator_approval_intent_hash_payload`,
`validate_operator_approval_intent_scalars`, `validate_operator_approval_intent_object`) and
standalone verifier `verify_operator_approval_intent_payload` plus CLI mode
`--verify-operator-approval-intent` (stdin-only; VALID = schema/semantic/hash only — not
authentication/consumption/activation). See
`PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_VERIFICATION_CONTRACT.md`.

The existing CLI mode `--freshness-preflight-activation-candidate` is unchanged — it still emits
evidence digest fields only.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_CLI_CONTRACT.md` — CLI stdout intent generation (RTM-7c.4p)
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_INTENT_VERIFICATION_CONTRACT.md` — standalone intent verification (RTM-7c.4q)
- `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_EVIDENCE_CONTRACT.md` — evidence schema v2 / hash binding
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition wiring root

## Still OPEN (unchanged posture)

Operator approval consumption, intent persistence/file output, signing/HMAC, Operator identity
authentication, replay/nonce/idempotency, approval pre-consumption revalidation, activation
token/caller, `--run`, KIS/network, broker/order, operational DB write, writer-stop machine
proof, default runtime activation beyond constant NO-GO.
